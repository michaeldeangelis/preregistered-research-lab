import json
import sys

from rlab import envs
from rlab import questions as Q
from rlab.kernel import Kernel
from rlab.project import init_project, load_project
from rlab.worker import fake_worker

from test_kernel import FAKES, triage


def test_profile_and_python(tmp_path):
    assert envs.profile_for(None) == "base"
    assert envs.profile_for(["sklearn"]) == "ml"
    assert envs.profile_for(["sklearn", "torch"]) == "torch"
    assert envs.profile_for(["llm"]) == "base"
    py, real = envs.python_for("ml", tmp_path)
    assert py == sys.executable and real is False
    (tmp_path / "ml" / "bin").mkdir(parents=True)
    (tmp_path / "ml" / "bin" / "python").write_text("")
    assert envs.python_for("ml", tmp_path) == (str(tmp_path / "ml" / "bin" / "python"), True)
    assert envs.unmet_gates(["llm", "torch", "local-logs"], {"allow_llm_calls": True}) == ["allow_local_logs"]


def test_gated_question_blocks_at_triage_and_env_reaches_script(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    init_project(root, "p")
    project = load_project(root)
    assert project.gates == {"allow_llm_calls": False, "allow_local_logs": False}
    src = tmp_path / "b.md"
    src.write_text("- Do agents overclaim? [type:compute] [needs:llm]\n- Does X predict Y? [type:compute] [needs:sklearn]\n")
    batch, ids = Q.import_batch(project, src)
    Q.approve_batch(project, batch)
    prompts = {}

    def script(call):
        prompts[call.prompt.split("\n")[1]] = call.prompt
        return FAKES["script"](call)

    k = Kernel(project, fake_worker({**FAKES, "triage": lambda c: {**triage(c), "type": "compute"}, "script": script}),
               tmp_path / "reg.jsonl", python=sys.executable)
    k.run()
    gated, normal = Q.load(project, ids[0]), Q.load(project, ids[1])
    assert gated["state"] == "blocked" and "allow_llm_calls" in gated["blocked_reason"]
    assert normal["state"] == "reported" and normal["env"] == "ml"
    assert "Analysis environment: 'ml'" in next(iter(prompts.values()))
