from .alm import ALM
import openai
import time
import os
import tiktoken
from functools import partial


class OpenAI(ALM):

    def __init__(self, model_path_or_name, openai_key=None, verbose=0, n_ctx=2048, **kwargs):
        super().__init__(model_path_or_name, n_ctx=n_ctx, verbose=verbose)
        if openai_key:
            openai.api_key = openai_key
        elif not "OPENAI_API_KEY" in os.environ:
            raise Exception("No openai key set!")

        conv = {"gpt3": "gpt-3.5-turbo", "gpt-3": "gpt-3.5-turbo", "chatgpt": "gpt-3.5-turbo", "gpt4": "gpt-4",
                "gpt-16k": "gpt-3.5-turbo-16k"}
        self.model_path_or_name = conv.get(model_path_or_name, model_path_or_name)
        self.symbols["ASSISTANT"] = "assistant"
        self.symbols["USER"] = "user"
        self.symbols["SYSTEM"] = "system"
        self.finish_meta = {}
        self.pricing = {"gpt-3.5-turbo": {"input": 0.0015, "output": 0.002},
                        "gpt-3.5-turbo-16k": {"input": 0.003, "output": 0.004},
                        "gpt-4": {"input": 0.03, "output": 0.06}}

    # @abstractmethod
    def tokenize(self, text):
        encoding = tiktoken.encoding_for_model(self.model_path_or_name)
        return encoding.encode(text)

    def tokenize_as_str(self, text):
        encoding = tiktoken.encoding_for_model(self.model_path_or_name)
        encoded = encoding.encode(text)
        return [encoding.decode_single_token_bytes(token).decode("utf-8") for token in encoded]

    def get_n_tokens(self, text):
        return len(self.tokenize(text))

    def _extract_message_from_generator(self, gen):

        for i in gen:
            try:
                token = i["choices"][0]["delta"]["content"]
            except:
                self.finish_meta["finish_reason"] = i["choices"][0]["finish_reason"]
            print(token, end ="")
            # self.test_txt += token
            yield token, None

    def create_native_generator(self, text, stream=True, keep_dict=False,token_prob_delta = None,
                                token_prob_abs = None, **kwargs):
        if token_prob_abs:
            raise Exception("OpenAI API only supports relative logit chance change")
        if token_prob_delta:
            response = openai.ChatCompletion.create(
                model=self.model_path_or_name,
                messages=text,
                stream=stream,
                logit_bias = token_prob_delta,
                **kwargs
            )
        else:
            response = openai.ChatCompletion.create(
                model=self.model_path_or_name,
                messages=text,
                stream=stream,
                **kwargs
            )

        if keep_dict:
            return response
        else:
            if stream:
                return self._extract_message_from_generator(response, stream=stream)
            else:
                return response["choices"][0]["message"]["content"]

    def build_prompt(self):
        prompt = []
        if "content" in self.system_msg:
            prompt.append({"role": self.symbols["SYSTEM"], "content":
                            self._replace_symbols(self.system_msg["content"])})

        for i in self.conv_history:
            prompt.append({"role": self.symbols[str(i["role"])], "content":  self._replace_symbols(i["content"])})
        return prompt
