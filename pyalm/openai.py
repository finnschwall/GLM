from .alm import ALM
from openai import OpenAI as _OpenAI
import time
import os
import tiktoken
from functools import partial
from warnings import warn
from timeit import default_timer as timer


class OpenAI(ALM):
    available_models = ["gpt-3.5-turbo", "gpt-3.5-turbo-16k", "gpt-4"]
    """All models that are known to work"""
    pricing = {"gpt-3.5-turbo": {"input": 0.0015, "output": 0.002},
               "gpt-3.5-turbo-16k": {"input": 0.003, "output": 0.004},
               "gpt-4": {"input": 0.03, "output": 0.06}, }
    """Pricing per input and output tokens in a prompt. Streaming costs the same"""
    pricing_meta = {"currency": "$", "token_unit": 1000}


    def get_available_models(self):
        return self.client.models.list().data

    def __init__(self, model_path_or_name, openai_key=None, verbose=0, **kwargs):
        super().__init__(model_path_or_name, verbose=verbose)


        conv = {"gpt3": "gpt-3.5-turbo", "gpt-3": "gpt-3.5-turbo", "chatgpt": "gpt-3.5-turbo", "gpt4": "gpt-4",
                "gpt-16k": "gpt-3.5-turbo-16k"}
        self.model = conv.get(model_path_or_name, model_path_or_name)

        if openai_key:
            self.client = _OpenAI(api_key=openai_key)
        elif "OPENAI_API_KEY" in os.environ:
            self.client = _OpenAI()
        else:
            raise Exception("No openai key set!")

        openai_specifics = {"ASSISTANT": "assistant", "USER": "user", "SYSTEM": "system"}
        self._built_in_symbols.update(openai_specifics)
        self.settings.prompt_obj_is_str = False

    # @abstractmethod
    def tokenize(self, text):
        encoding = tiktoken.encoding_for_model(self.model)
        return encoding.encode(text)

    def tokenize_as_str(self, text):
        encoding = tiktoken.encoding_for_model(self.model)
        encoded = encoding.encode(text)
        return [encoding.decode_single_token_bytes(token).decode("utf-8") for token in encoded]

    # TODO add static variant
    def get_n_tokens(self, text):
        return len(self.tokenize(text))

    def _extract_message_from_generator(self, gen):

        for i in gen:
            try:
                token = i.choices[0].delta.content
                finish_reason =  i.choices[0].finish_reason
                if finish_reason:
                    self.finish_meta["finish_reason"] = finish_reason
                if token is None:
                    break
                yield token, None
            except:
                pass
            # self.test_txt += token


    def create_native_completion(self, text, max_tokens=256, stop=None, keep_dict=False, token_prob_delta=None,
                                 token_prob_abs=None,
                                 log_probs=None, **kwargs):
        if isinstance(text, str):
            raise Exception("Native OpenAI call only supports calls via a json chat format")
        if token_prob_abs:
            raise Exception("OpenAI API only supports relative logit chance change")
        if log_probs:
            raise Exception("OpenAI API does not support retrieval of logits")
        start = timer()
        if token_prob_delta:
            response = self.client.chat.completions.create(model=self.model,
            messages=text,
            logit_bias=token_prob_delta,
            stop=stop,
            **kwargs)
        else:
            response = self.client.chat.completions.create(model=self.model,
            messages=text,
            stop=stop,
            **kwargs)
        response_txt = response.choices[0].message.content
        end = timer()

        self.finish_meta["finish_reason"] = response.choices[0].finish_reason
        tok_in = response.usage.prompt_tokens
        tok_gen = response.usage.completion_tokens
        tok_total = response.usage.total_tokens

        self.finish_meta["tokens"] = {"prompt_tokens": tok_in, "generated_tokens": tok_gen, "total_tokens": tok_total}
        self.finish_meta["timings"] = {"total_time": round(end - start,3)}
        self.finish_meta["t_per_s"] = {"token_total_per_s": (tok_total) / (end - start)}

        try:
            cost_in = OpenAI.pricing[self.model]["input"] * tok_in / OpenAI.pricing_meta["token_unit"]
            cost_out = OpenAI.pricing[self.model]["output"] * tok_gen / OpenAI.pricing_meta["token_unit"]
            self.finish_meta["cost"] = {"input": round(cost_in, 3), "output": round(cost_out, 5),
                                        "total": round(cost_out + cost_in, 5),
                                        "total_cent": round((cost_out + cost_in) * 100, 3),
                                        "unit": OpenAI.pricing_meta["currency"]}
        except:
            pass

        if keep_dict:
            return response
        return response_txt

    def create_native_generator(self, text, keep_dict=False, token_prob_delta=None,
                                token_prob_abs=None, **kwargs):
        if token_prob_abs:
            raise Exception("OpenAI API only supports relative logit chance change")
        if token_prob_delta:
            response = self.client.chat.completions.create(model=self.model,
            messages=text,
            stream=True,
            logit_bias=token_prob_delta,
            # stop_sequences=stop,
            **kwargs)
        else:
            response = self.client.chat.completions.create(model=self.model,
            messages=text,
            stream=True,
            **kwargs)

        if keep_dict:
            return response
        else:
            return self._extract_message_from_generator(response)

    def build_prompt(self, conv_history=None, system_msg=None):
        if not conv_history:
            conv_history = self.conversation_history.tracker
        if not system_msg:
            system_msg = self.conversation_history.system_message
        prompt = []
        if system_msg and system_msg != "":
            prompt.append({"role": self.symbols["SYSTEM"], "content":
                self.replace_symbols(system_msg)})
        for i in conv_history:
            prompt.append({"role": self.symbols[str(i["role"])], "content": self.replace_symbols(i["content"], i)})
        return prompt
