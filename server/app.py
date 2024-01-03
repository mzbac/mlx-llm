from threading import Lock
import time
from typing import Optional
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from .types import (
    ChatCompletionRequest,
    CompletionRequestMessage,
    CompletionUsage,
    CreateChatCompletionResponse,
    CreateChatCompletionStreamResponse,
)
from sse_starlette.sse import EventSourceResponse
from pathlib import Path
import json
import uuid
from transformers import AutoTokenizer
from mlx.utils import tree_unflatten
import mlx.core as mx
import mlx.nn as nn
from llm.llama.llama import Llama, ModelArgs

_llama_model: Optional[Llama] = None

llama_outer_lock = Lock()
llama_inner_lock = Lock()


def set_llama_model(model: Llama):
    global _llama_model
    _llama_model = model


def get_llama_model():
    llama_outer_lock.acquire()
    release_outer_lock = True
    try:
        llama_inner_lock.acquire()
        try:
            llama_outer_lock.release()
            release_outer_lock = False
            yield _llama_model
        finally:
            llama_inner_lock.release()
    finally:
        if release_outer_lock:
            llama_outer_lock.release()


def load_model(model_path: str, disable_fast_tokenizer: bool):
    model_path = Path(model_path)
    with open(model_path / "config.json", "r") as f:
        config = json.load(f)
        config.pop("model_type")
        quantization = config.pop("quantization", None)
        model_args = ModelArgs(**config)

    model = Llama(model_args)
    weights = mx.load(str(model_path / "weights.npz"))
    if quantization is not None:
        nn.QuantizedLinear.quantize_module(model, **quantization)
    model.update(tree_unflatten(list(weights.items())))

    if disable_fast_tokenizer:
        tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=False)
    else:
        tokenizer = AutoTokenizer.from_pretrained(model_path)
    return model, tokenizer


def generate(
    prompt: mx.array,
    model: Llama,
    temp: float = 0.0,
):
    def sample(logits):
        if temp == 0:
            return mx.argmax(logits, axis=-1)
        else:
            return mx.random.categorical(logits * (1 / temp))

    y = prompt
    cache = None
    while True:
        logits, cache = model(y[None], cache=cache)
        logits = logits[:, -1, :]
        y = sample(logits)
        yield y


def convert_chat(messages: CompletionRequestMessage):
    # TODO change the hardcoded mapping
    prompt = ""
    system = ""
    user = "### Instruction: "
    assistant = "### Response: "
    for line in messages:
        if line.role == "system":
            prompt += f"{system}{line.content}\n"
        if line.role == "user":
            prompt += f"{user}{line.content}\n"
        if line.role == "assistant":
            prompt += f"{assistant}{line.content}\n"
    prompt += assistant.rstrip()
    return prompt


def create_app(model_path: str, disable_fast_tokenizer: bool):
    model, tokenizer = load_model(model_path, disable_fast_tokenizer)
    set_llama_model(model)
    app = FastAPI()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/v1/chat/completions")
    async def chat_completions(
        _: Request,
        body: ChatCompletionRequest,
        model: Llama = Depends(get_llama_model),
    ):
        chat_id = f"chatcmpl-{uuid.uuid4()}"
        prompt = tokenizer(
            convert_chat(body.messages),
            return_tensors="np",
            return_attention_mask=False,
        )["input_ids"][0]
        prompt = mx.array(prompt)

        if body.stream:

            async def event_generator():
                for token, _ in zip(
                    generate(prompt, model, body.temperature),
                    range(body.max_tokens),
                ):
                    if token == tokenizer.eos_token_id:
                        break
                    s = tokenizer.decode(token.item())
                    response = CreateChatCompletionStreamResponse(
                        id=chat_id,
                        object="chat.completion.chunk",
                        created=int(time.time()),
                        model="gpt-3.5-turbo",
                        system_fingerprint=f"fp_{uuid.uuid4()}",
                        choices=[
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": s},
                                "logprobs": None,
                                "finish_reason": None,
                            }
                        ],
                    )

                    yield f"{json.dumps(response)}"

            return EventSourceResponse(event_generator())
        else:
            tokens = []
            for token, _ in zip(
                generate(prompt, model, body.temperature),
                range(body.max_tokens),
            ):
                if token == tokenizer.eos_token_id:
                    break
                tokens.append(token.item())
            s = tokenizer.decode(tokens)
            response = CreateChatCompletionResponse(
                id=chat_id,
                object="chat.completion",
                created=int(time.time()),
                model="gpt-3.5-turbo",
                system_fingerprint=f"fp_{uuid.uuid4()}",
                choices=[
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": s},
                        "logprobs": None,
                        "finish_reason": None,
                    }
                ],
                usage=CompletionUsage(
                    prompt_tokens=len(prompt),
                    completion_tokens=len(tokens),
                    total_tokens=len(prompt) + len(tokens),
                ),
            )
            return f"{json.dumps(response)}"

    return app
