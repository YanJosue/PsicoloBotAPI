"""Lazy LoRA text generator.

The heavy ML stack (``unsloth`` / ``torch``) is imported only when the model
is actually loaded, so importing the API (and running its tests) needs
neither a GPU nor those packages.
"""

import os
import threading

LORA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "lora_psicolobot_v3"
)


class LoraGenerator:
    """Loads the fine-tuned model on first use and generates completions."""

    def __init__(self, lora_dir: str = LORA_DIR):
        self.lora_dir = lora_dir
        self._model = None
        self._tokenizer = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Load the model onto the GPU. Safe to call more than once."""
        with self._lock:
            if self.loaded:
                return
            from unsloth import FastLanguageModel

            print("Cargando modelo PsicoloBot...")
            model, tokenizer = FastLanguageModel.from_pretrained(
                model_name=self.lora_dir,
                max_seq_length=2048,
                dtype=None,
                load_in_4bit=True,
            )
            FastLanguageModel.for_inference(model)
            self._tokenizer = tokenizer
            self._model = model
            print("✅ Modelo listo")

    def generate(self, messages: list[dict]) -> str:
        """Return the raw decoded completion for a chat ``messages`` list."""
        self.load()
        import torch

        tokenizer = self._tokenizer

        # Use the model's native chat template (Llama 3 Instruct).
        prompt_final = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        inputs = tokenizer([prompt_final], return_tensors="pt").to("cuda")

        with torch.no_grad():
            out = self._model.generate(
                **inputs,
                max_new_tokens=350,
                do_sample=False,  # Greedy decoding for precision
                repetition_penalty=1.2,  # Avoid repetitive answers
                pad_token_id=tokenizer.eos_token_id,
            )

        n = inputs["input_ids"].shape[1]
        return tokenizer.decode(out[0][n:], skip_special_tokens=True).strip()
