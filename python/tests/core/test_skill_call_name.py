"""a Skill call's name is always recoverable (span attribute first)."""

from agentplay._core.ingestion.coding_agent.skill_names import (
    SKILL_CALL_NAME_ATTR,
    skill_call_attributes,
    skill_call_name,
)


def test_attribute_is_set_only_for_skill_calls():
    assert skill_call_attributes("Skill", {"skill": "myplugin:deploy", "args": "x"}) == {
        SKILL_CALL_NAME_ATTR: "myplugin:deploy"
    }
    assert skill_call_attributes("activate_skill", {"command": "entloop"}) == {
        SKILL_CALL_NAME_ATTR: "entloop"
    }
    assert skill_call_attributes("Bash", {"command": "ls"}) == {}
    assert skill_call_attributes("Skill", "not-a-dict") == {}


def test_name_survives_a_truncated_input_value():
    truncated = '{"skill": "myplugin:deploy", "args": "' + "x" * 3000
    assert skill_call_name("Skill", {SKILL_CALL_NAME_ATTR: "myplugin:deploy"}, truncated[:2000]) == (
        "myplugin:deploy"
    )


def test_falls_back_to_input_json_and_strips_a_slash():
    assert skill_call_name("Skill", {}, '{"skill": "/security-review"}') == "security-review"


def test_junk_and_non_skill_calls_are_none():
    assert skill_call_name("Skill", {SKILL_CALL_NAME_ATTR: "common"}) is None
    assert skill_call_name("Read", {SKILL_CALL_NAME_ATTR: "entloop"}) is None
    assert skill_call_name("Skill", None, "{not json") is None
