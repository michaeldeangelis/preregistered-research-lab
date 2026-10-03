from rlab import questions as Q
from rlab.project import init_project, load_project
from rlab.screen import apply, replacements_section

SCREEN_MD = """# screen
| id | verdict |
|---|---|
- SOLVED: 1
- FRONTIER: 1

## Replacements
- New open question? [type:compute] [family:f] [decision:d] [prior:arXiv:2401.1] [gap:nobody did X]

## Notes
- not a question
"""


def test_replacements_section_only():
    items = Q.parse_import(replacements_section(SCREEN_MD))
    assert [i["text"] for i in items] == ["New open question?"]


def test_apply_removes_annotates_and_imports(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    init_project(root, "p")
    project = load_project(root)
    src = tmp_path / "b.md"
    src.write_text("- Solved q? [type:lit]\n- Open q? [type:compute]\n- Approved solved q? [type:lit]\n")
    _, ids = Q.import_batch(project, src)
    q3 = Q.load(project, ids[2])
    Q.set_state(project, q3, "approved")
    screen = [{"id": ids[0], "verdict": "SOLVED", "prior": "doi:10.1/x", "reason": "settled"},
              {"id": ids[1], "verdict": "FRONTIER", "prior": "arXiv:2501.2", "gap": "no causal test"},
              {"id": ids[2], "verdict": "SOLVED", "reason": "settled"}]
    out = apply(project, screen, SCREEN_MD)
    assert out == {"annotated": 1, "removed": 1, "created": 1, "kept_locked": [ids[2]], "unscreened": []}
    assert not Q.qdir(project, ids[0]).exists()
    assert Q.load(project, ids[1])["gap"] == "no causal test"
    log = (project.qbank / "SCREEN.md").read_text()
    assert "SOLVED: Solved q?" in log and "doi:10.1/x" in log and "approved or started" in log
    new = [q for q in Q.all_questions(project) if q["import_batch"] == "frontier-v1"]
    assert len(new) == 1 and new[0]["prior_work"] == "arXiv:2401.1"
