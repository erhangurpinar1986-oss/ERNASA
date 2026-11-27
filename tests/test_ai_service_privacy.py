"""Every OpenAI Responses call in services/ai_service.py must send store=False (no provider-side retention)."""
import ast
from pathlib import Path


def test_every_responses_call_disables_storage():
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath('services/ai_service.py').read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'create'
             and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == 'responses']
    assert calls, 'expected at least one responses.create call'
    for call in calls:
        store = [k for k in call.keywords if k.arg == 'store']
        assert store and isinstance(store[0].value, ast.Constant) and store[0].value.value is False, ast.dump(call)[:200]
