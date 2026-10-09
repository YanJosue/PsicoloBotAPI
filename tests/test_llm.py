"""Tests for the lazy LoRA generator using stubbed ``unsloth`` and ``torch``.

The real libraries need a CUDA GPU, so these tests inject stub modules and
assert that loading and generation use exactly the same parameters the API
used before the refactor.
"""

import contextlib
import sys
import types
from pathlib import Path

import pytest

EXPECTED_LORA_DIR = str(
    Path(__file__).resolve().parent.parent / "lora_psicolobot_v3"
)


class FakeInputIds:
    shape = (1, 2)


class FakeInputs(dict):
    """Mimics the tokenizer's BatchEncoding: a mapping with ``.to(device)``."""

    def __init__(self):
        super().__init__(input_ids=FakeInputIds(), attention_mask="mask")
        self.device = None

    def to(self, device):
        self.device = device
        return self


class FakeTokenizer:
    eos_token_id = 7

    def __init__(self):
        self.template_calls = []
        self.tokenize_calls = []
        self.inputs = FakeInputs()
        self.decoded = None

    def apply_chat_template(self, messages, **kwargs):
        self.template_calls.append((messages, kwargs))
        return "PROMPT"

    def __call__(self, prompts, **kwargs):
        self.tokenize_calls.append((prompts, kwargs))
        return self.inputs

    def decode(self, tokens, **kwargs):
        self.decoded = (list(tokens), kwargs)
        return '  {"riesgo": "BAJO"}  '


class FakeModel:
    def __init__(self):
        self.generate_kwargs = None

    def generate(self, **kwargs):
        self.generate_kwargs = kwargs
        return [[10, 11, 12, 13]]


@pytest.fixture
def stub_ml(monkeypatch):
    model, tokenizer = FakeModel(), FakeTokenizer()
    state = {"from_pretrained": None, "for_inference": None}

    class FastLanguageModel:
        @staticmethod
        def from_pretrained(**kwargs):
            state["from_pretrained"] = kwargs
            return model, tokenizer

        @staticmethod
        def for_inference(loaded_model):
            state["for_inference"] = loaded_model

    unsloth = types.ModuleType("unsloth")
    unsloth.FastLanguageModel = FastLanguageModel
    torch = types.ModuleType("torch")
    torch.no_grad = contextlib.nullcontext
    monkeypatch.setitem(sys.modules, "unsloth", unsloth)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return types.SimpleNamespace(model=model, tokenizer=tokenizer, state=state)


def test_generator_does_not_load_anything_on_construction(stub_ml):
    import llm

    generator = llm.LoraGenerator()

    assert generator.loaded is False
    assert stub_ml.state["from_pretrained"] is None


def test_load_uses_the_same_parameters_as_before_the_refactor(stub_ml):
    import llm

    generator = llm.LoraGenerator()
    generator.load()

    assert generator.loaded is True
    assert stub_ml.state["from_pretrained"] == {
        "model_name": EXPECTED_LORA_DIR,
        "max_seq_length": 2048,
        "dtype": None,
        "load_in_4bit": True,
    }
    assert stub_ml.state["for_inference"] is stub_ml.model


def test_load_is_idempotent(stub_ml):
    import llm

    generator = llm.LoraGenerator()
    generator.load()
    stub_ml.state["from_pretrained"] = None
    generator.load()

    assert stub_ml.state["from_pretrained"] is None


def test_generate_loads_lazily_and_decodes_only_the_new_tokens(stub_ml):
    import llm

    generator = llm.LoraGenerator()
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

    raw = generator.generate(messages)

    assert generator.loaded is True
    assert raw == '{"riesgo": "BAJO"}'
    tokenizer = stub_ml.tokenizer
    assert tokenizer.template_calls == [
        (messages, {"tokenize": False, "add_generation_prompt": True})
    ]
    assert tokenizer.tokenize_calls == [(["PROMPT"], {"return_tensors": "pt"})]
    assert tokenizer.inputs.device == "cuda"
    assert stub_ml.model.generate_kwargs == {
        "input_ids": tokenizer.inputs["input_ids"],
        "attention_mask": "mask",
        "max_new_tokens": 350,
        "do_sample": False,
        "repetition_penalty": 1.2,
        "pad_token_id": 7,
    }
    # input_ids has 2 tokens, so only tokens after index 2 are decoded.
    assert tokenizer.decoded == ([12, 13], {"skip_special_tokens": True})
