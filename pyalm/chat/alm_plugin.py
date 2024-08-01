import datetime
import json
import re
import requests

from rixaplugin import variables as var
from rixaplugin.decorators import global_init, worker_init, plugfunc
from rixaplugin import worker_context, execute, async_execute

from rixaplugin.internal.memory import _memory

import rixaplugin.async_api as api

from pyalm.models.openai import OpenAI
from pyalm import ConversationTracker, ConversationRoles

import logging
from rixaplugin.internal import api as internal_api
import os
import aiohttp

llm_logger = logging.getLogger("rixa.llm_server")

# from rixaplugin.examples import knowledge_base

import time

openai_key = var.PluginVariable("OPENAI_KEY", str, readable=var.Scope.LOCAL)
deepl_key = var.PluginVariable("DEEPL_KEY", str, readable=var.Scope.LOCAL)
max_tokens = var.PluginVariable("MAX_TOKENS", int, 4096, readable=var.Scope.LOCAL)
chat_store_loc = var.PluginVariable("chat_store_loc", str, default=None)
multiplexing = var.PluginVariable("multiplexing", bool, default=False, readable=var.Scope.USER, writable=var.Scope.USER,
                                  user_facing_name="Enable Multiplexing")
translation_layer = var.PluginVariable("translation_layer", str, default="None", options=["None", "deepl", "LLM"],
                                       readable=var.Scope.USER, writable=var.Scope.USER)
enable_knowledge_retrieval_var = var.PluginVariable("enable_knowledge_retrieval", bool, default=True,
                                                    readable=var.Scope.USER, writable=var.Scope.USER)
nlp_engine = var.PluginVariable("nlp_engine", str, default="openai", readable=var.Scope.USER,
                                writable=var.Scope.USER, options=["openai","auto", "llama","gemini"])


async def translate_message(message, context=None, target_lang="DE"):
    url = 'https://api-free.deepl.com/v2/translate'
    data = {
        "auth_key": deepl_key.get(),
        "text": message,
        "target_lang": target_lang
    }
    if context:
        data["context"] = context
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data) as response:
            response_json = await response.json()
            return response_json["translations"][0]["text"]


@plugfunc()
async def generate_text(conversation_tracker_yaml, enable_function_calling=True, enable_knowledge_retrieval=True,
                        knowledge_retrieval_domain=None, system_msg=None, username=None):
    """
    Generate text based on the conversation tracker and available functions

    :param conversation_tracker_yaml: The conversation tracker in yaml format
    :param available_functions: A list of available functions
    """
    user_api = internal_api.get_api()
    # api.display_in_chat(text="Starting preprocessing...", role="partial")
    # if username and chat_store_loc.get():
    #     api.datalog_to_tmp(f"New message at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if "excluded_functions" not in user_api.scope:
        user_api.scope["excluded_functions"] = ["generate_text", "get_total_tokens"]

    tracker = ConversationTracker.from_yaml(conversation_tracker_yaml)

    use_multiplexing = multiplexing.get()
    translation_val = translation_layer.get()
    if translation_val == "deepl":
        # translate last message to english for chatbot
        tracker[-1]["translated_content"] = tracker[-1]["content"]
        last_bot_msg = tracker.get_last_message(ConversationRoles.ASSISTANT)
        context = None if last_bot_msg is None else last_bot_msg
        tracker[-1]["content"] = await translate_message(tracker[-1]["content"], target_lang="EN", context=context)
    if translation_val == "LLM":
        tracker[-1]["translated_content"] = tracker[-1]["content"]
        kwargs = {"conversation_history": conversation_tracker_yaml, "to_english": True}
        future = await async_execute("translate_last_message", "llm_server", kwargs=kwargs, return_future=True)
        translated_msg = await future
        tracker[-1]["content"] = translated_msg

    last_usr_msg = tracker.get_last_message(ConversationRoles.USER)

    enable_knowledge_retrieval = enable_knowledge_retrieval_var.get() & enable_knowledge_retrieval

    if use_multiplexing:
        kwargs = {"conversation_history": conversation_tracker_yaml,
                  "knowledge_retrieval_domain": knowledge_retrieval_domain,
                  "system_msg": system_msg}
        future = await async_execute("get_preprocessing_json", "llm_server", kwargs=kwargs, return_future=True)
        preprocessor_json = await future
        if preprocessor_json is not None:
            enable_function_calling = preprocessor_json["enable_function_calling"]
            enable_knowledge_retrieval = preprocessor_json["use_document_retrieval"]
            info_score = preprocessor_json["info_score"]
            queries = preprocessor_json["queries"]
            included_functions = preprocessor_json["included_functions"]

    context = None
    context_str = None
    if enable_knowledge_retrieval is True:
        try:
            future = await async_execute("query_db", args=[last_usr_msg["content"], 4], kwargs={"min_score": 0.5,
                                                                                                "max_chars": 5000},
                                         return_future=True)
            context, scores = await future
            context_str = ""
            for i in context:
                context_str += f"ID: {i['index']}\nDOC TITLE: {i['document_title']}\nHEADER: {i['header']}\n" \
                               f"CONTENT: {i['content']}\n"
        except Exception as e:
            await api.show_message("Knowledge retrieval system faulty. No context available.")
            llm_logger.exception(f"Could not retrieve context from knowledge base")
        # llm.include_context_msg = True
    else:
        context_str = None
        # llm.include_context_msg = False

    if enable_function_calling:
        func_list = _memory.get_functions_as_str(user_api.scope, short=False)
        # llm.include_function_msg = True
    else:
        func_list = None
        # llm.include_function_msg = False
    print(enable_function_calling,func_list)
    kwargs = {"conv_tracker": tracker, "context": context_str, "func_list": func_list, "system_msg": system_msg,
              "username": username,
              "chat_store_loc": chat_store_loc,
              "temp": 0}
    future = await async_execute("create_completion_plugin", "llm_server", kwargs=kwargs, return_future=True)
    tracker = await future
    assistant_msgs = tracker.pop_entry()

    all_citations = []
    total_content = ""
    code_calls = []
    for i, msg in enumerate(assistant_msgs[::-1]):
        if "content" in msg:
            total_content += msg["content"]
            if i != len(assistant_msgs) - 1:
                total_content += "\n"
            citations = re.findall(r"\{\{(\d+)\}\}", msg["content"])
            try:
                citation_ids = [int(c) for c in citations]
                all_citations += citation_ids
            except Exception as e:
                llm_logger.exception(f"Could not parse citation ids")
        if "code" in msg:
            if "return_value" in msg:
                code_calls.append({"code": msg["code"], "return": msg["return_value"]})
            else:
                code_calls.append({"code": msg["code"]})
    convo_idx = 0 if len(tracker.tracker) == 0 else tracker.tracker[-1]["index"] + 1
    used_citations = []
    if context:
        for i in context:
            if i["index"] in all_citations:
                used_citations.append(i)
                # replace citations with markdow link
                total_content = re.sub(r"\{\{" + str(i["index"]) + r"\}\}",
                                       f"[[{i['document_title']}/{i['header']}]](javascript:showCitation({convo_idx},{i['index']}))",
                                       total_content)

    code_str = ""
    if len(code_calls) == 1:
        code_str = code_calls[0]["code"]
        if "return" in code_calls[0]:
            code_str += f"\nRETURN:\n{code_calls[0]['return']}"
    if len(code_calls) > 1:
        for i, code in enumerate(code_calls):
            code_str += f"CALL {i}\n{code['code']}\n"
            if "return" in code:
                code_str += f"RETURN:\n{code['return']}\n"

    merged_tracker_entry = {"role": ConversationRoles.ASSISTANT,
                            "content": total_content, }
    if len(used_citations) > 0:
        merged_tracker_entry["citations"] = used_citations
    if code_str:
        merged_tracker_entry["code"] = code_str
    # print("\n\n")
    # from pprint import pp
    # pp(merged_tracker_entry, width=120)
    merged_tracker_entry["index"] = convo_idx
    # merged_tracker_entry["metadata"] = llm.finish_meta
    # merged_tracker_entry["processing"] = preprocessor_json
    if translation_val == "deepl":
        merged_tracker_entry["translated_content"] = await translate_message(merged_tracker_entry["content"],
                                                                             target_lang="DE")
    tracker.tracker.append(merged_tracker_entry)
    if translation_val == "LLM":
        kwargs = {"conversation_history": tracker.to_yaml(), "to_english": False}
        future = await async_execute("translate_last_message", "llm_server", kwargs=kwargs, return_future=True)
        translated_msg = await future
        print("ORIGINAL", merged_tracker_entry["content"])
        print("TRANSLATED", translated_msg)
        tracker[-1]["translated_content"] = translated_msg


    # api.datalog_to_tmp(f"\n\n\n{tracker.to_yaml()}")

    return tracker.to_yaml()
