import sys
import types
import unittest

sys.modules.setdefault("huggingface_hub", types.SimpleNamespace(hf_hub_download=lambda **kwargs: None))
sys.modules.setdefault("llama_cpp", types.SimpleNamespace(Llama=object))

from run_rubric_evaluator import infer, prompt, response_schema


class FakeModel:
    def create_chat_completion(self, **kwargs):
        self.call = kwargs
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}}


class RubricEvaluatorContractTests(unittest.TestCase):
    def test_every_variant_constrains_exact_root_structure(self):
        for variant, expected in (
            ("A", {"importance", "description", "evidence_control", "summary", "justification"}),
            ("B", {"importance", "description", "evidence_control", "summary", "evidence"}),
            ("C", {"importance", "justification"}),
        ):
            with self.subTest(variant=variant):
                schema = response_schema(variant)
                self.assertEqual(set(schema["required"]), expected)
                self.assertFalse(schema["additionalProperties"])
                model = FakeModel()
                infer(model, variant, "An abstract with a concrete result.")
                self.assertEqual(model.call["response_format"]["schema"], schema)
        self.assertEqual(set(response_schema("B")["properties"]["evidence"]["required"]),
                         {f"I{i}" for i in range(1, 8)})

    def test_prompt_names_nested_objects_explicitly(self):
        text = prompt("A", "A reported result.")
        self.assertIn('"importance" OBJECT', text)
        self.assertIn('ROOT "evidence_control"', text)
        self.assertIn('"justification"', text)


if __name__ == "__main__":
    unittest.main()
