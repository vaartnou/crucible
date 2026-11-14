# pyright: basic
"""Cala provenance, multi-site dynamics and public-tool replay contracts."""

import asyncio
import copy
import hashlib
import json
import math
from importlib.resources import files
from typing import Any

import numpy as np
import pytest

from crucible.core.registry import get_policy, get_task
from crucible.core.tools import Observation, ToolCall
from crucible.envs.supplygym.cala_task import SupplyGymCalaTask, cala_scoring_hash
from crucible.envs.supplygym.cala_world import ASSUMPTIONS, cala_world_identity, make_cala_world
from crucible.envs.supplygym.grader import (
    SCORING_HASH,
    grade_play,
    replay_default,
    replay_reference,
)
from crucible.envs.supplygym.play import CALA_REGIME, Play
from crucible.envs.supplygym.policies.baselines import Availability
from crucible.envs.supplygym.policies.reference import (
    CALA_REFERENCE,
    cala_policy_identity,
    make_decider,
)
from crucible.envs.supplygym.policies.views import StatusView, to_vector
from crucible.envs.supplygym.sim import ACT_DIM
from crucible.envs.supplygym.task import ControlsArgs, SupplyGymFreshTask, SupplyGymTask
from crucible.envs.supplygym.world import GAMMA_PROP, REGIONS, _health, make_tape, tape_digest

SLICE_SHA256 = "25ed29a8e17833b63155f7f30ff1840d8611ab6a9788b3da1a60d2406a7bc63e"


def _pack() -> dict[str, Any]:
    return json.loads(
        files("crucible.envs.supplygym").joinpath("data/cala_world_v0.json").read_bytes()
    )


def test_availability_diagnostic_returns_valid_weights_even_at_full_outage() -> None:
    status = Play(1000, CALA_REGIME).status()
    suppliers = status["suppliers"]
    for supplier in suppliers:
        supplier.update(fill_ratio=0, risk=1)
    args = ControlsArgs.model_validate(Availability().decide(status))
    assert args.supplier_weights is not None
    assert len(args.supplier_weights) == 10
    assert set(args.supplier_weights.values()) == {math.exp(-4)}


def test_packaged_slice_is_self_contained_canonical_and_hash_pinned() -> None:
    raw = files("crucible.envs.supplygym").joinpath("data/cala_world_v0.json").read_bytes()
    pack = _pack()
    assert raw == (json.dumps(pack, indent=2, sort_keys=True) + "\n").encode()
    assert hashlib.sha256(raw).hexdigest() == SLICE_SHA256
    assert pack["contract_version"] == "0.2"
    sources = {source["id"]: source for source in pack["sources"]}
    assert len(sources) == len(pack["sources"])
    assert len({fact["id"] for fact in pack["facts"]}) == len(pack["facts"])
    for fact in pack["facts"]:
        assert fact["source_ids"]
        for source_id in fact["source_ids"]:
            source = sources[source_id]
            assert source["url"].startswith("https://")
            assert len(source["text_sha256"]) == 64
    # Verifying the installed artifact does not require git history or private inputs.
    assert cala_world_identity()["slice_sha256"] == SLICE_SHA256


def test_aliases_site_edges_and_synthetic_parameters_have_explicit_provenance() -> None:
    pack, world = _pack(), make_cala_world()
    facts = {fact["id"]: fact for fact in pack["facts"]}
    aliases = pack["direct_aliases"] + pack["alternative_aliases"]
    assert [node["name"] for node in world.nodes[:12]] == aliases
    assert [node["id"] for node in world.nodes[:12]] == [
        *(f"S{i:02d}" for i in range(1, 11)),
        "A01",
        "A02",
    ]
    assert world.n == 12 + sum(f["kind"] == "site" for f in facts.values())
    for index, alias in enumerate(aliases):
        node = world.nodes[index]
        fact = facts[node["fact_id"]]
        assert fact["subject_alias"] == alias
        assert pack["buyer_alias"] in fact["attributes"]["supplies"]
        assert node["source_ids"] == fact["source_ids"]
        assert sum(weight for _, weight in world.parents[index]) == pytest.approx(1.0)
        expected_sites = {
            f["id"]
            for f in facts.values()
            if f["kind"] == "site" and f["attributes"]["operator"] == alias
        }
        assert {
            world.nodes[parent]["fact_id"] for parent, _ in world.parents[index]
        } == expected_sites
        for parent, weight in world.parents[index]:
            site = world.nodes[parent]
            assert weight > 0 and site["source_ids"] == facts[site["fact_id"]]["source_ids"]
            assert site["country"] == facts[site["fact_id"]]["attributes"]["country"]
            assert "output_weight" in site["synthetic_fill"]
        assert {"lead", "price", "cap", "base_fail"} <= node["synthetic_fill"].keys()


def test_seeded_world_is_repeatable_and_site_weights_change_with_seed() -> None:
    a, b, other = make_cala_world(7), make_cala_world(7), make_cala_world(8)
    assert a == b
    assert a.parents != other.parents
    assert [node["fact_id"] for node in a.nodes] == [node["fact_id"] for node in other.nodes]


def test_opening_orders_are_prepaid_bookings_and_independent_of_episode_truth() -> None:
    p, other = Play(1000, CALA_REGIME), Play(1001, CALA_REGIME, "other")
    assert p.env.orders and other.env.orders == p.env.orders
    assert p.env.onhand > 0 and p.env.tot["purchase"] == p.env.tot["profit"] == 0
    assert p.env.onhand + p.env._pipeline() == round(p.env.levels(p.env.active(), 0)[0])
    for order in p.env.orders:
        assert order["qty"] == order["ordered"] > 0
        assert order["arr"] == order["nom"] >= 0
        assert order["sup"] in p.env.active()
    opening = p.env.onhand + sum(order["qty"] for order in p.env.orders)
    for _ in range(53):
        p.apply({})
    # Direct price is a common quote. All deliveries since reset are paid at that price.
    purchased = p.env.tot["purchase"] / p.env.price[0]
    closing = p.env.onhand + sum(order["qty"] for order in p.env.orders)
    assert opening + purchased == pytest.approx(closing + p.env.tot["sales"])


@pytest.mark.parametrize("region", REGIONS)
def test_region_losses_target_sites_and_propagate_only_weighted_exposure(region: str) -> None:
    world = make_cala_world()
    targets = world.region_nodes(region)
    assert targets and all(index >= 12 for index in targets)
    severity = np.zeros((2, world.n))
    severity[1, targets] = 1.0
    health = _health(world, severity)
    assert np.all(health[0] == 1)
    for supplier in range(12):
        exposure = sum(weight for parent, weight in world.parents[supplier] if parent in targets)
        assert health[1, supplier] == pytest.approx(1.0 - GAMMA_PROP * exposure)
    tape = make_tape(world, 1000)
    for event in tape.events:
        if event["type"] in ("strike", "port_closure", "sanction"):
            assert all(index >= 12 for index in event["nodes"])
            assert all(world.nodes[index]["region"] == event["region"] for index in event["nodes"])
            if event["type"] == "port_closure":
                assert all(world.nodes[index]["port"] for index in event["nodes"])


def test_site_specific_loss_spares_unrelated_supplier() -> None:
    world = make_cala_world()
    parent, weight = world.parents[0][0]
    severity = np.zeros((1, world.n))
    severity[0, parent] = 0.75
    health = _health(world, severity)
    assert health[0, 0] == pytest.approx(1 - GAMMA_PROP * weight * 0.75)
    assert health[0, 1] == 1.0


def test_salt_changes_tape_not_static_world_and_replays_keep_identity() -> None:
    plain, salted, repeated = (
        Play(1000, CALA_REGIME),
        Play(1000, CALA_REGIME, "abc"),
        Play(1000, CALA_REGIME, "abc"),
    )
    assert plain.env.tape is not None
    assert salted.env.tape is not None
    assert repeated.env.tape is not None
    assert plain.env.world == salted.env.world
    assert tape_digest(plain.env.tape) != tape_digest(salted.env.tape)
    assert tape_digest(salted.env.tape) == tape_digest(repeated.env.tape)
    for anchor in (replay_default(salted), replay_reference(salted)):
        assert anchor.env.tape is not None
        assert anchor.regime == CALA_REGIME and anchor.env.salt == "abc"
        assert anchor.env.world == salted.env.world
        assert tape_digest(anchor.env.tape) == tape_digest(salted.env.tape)
    expected = hashlib.sha256(
        json.dumps(
            {
                "scoring": SCORING_HASH,
                "world": cala_world_identity(),
                "policy": cala_policy_identity(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert SupplyGymCalaTask.scoring_hash == expected != SCORING_HASH
    canonical = json.dumps(ASSUMPTIONS, sort_keys=True, separators=(",", ":")).encode()
    assert cala_world_identity()["assumptions_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_public_status_excludes_hidden_strings_and_is_insensitive_to_future_numbers() -> None:
    play = Play(1000, CALA_REGIME, "secret")
    for _ in range(4):
        play.apply({})
    before = copy.deepcopy(play.status())
    assert play.env.tape is not None
    future = play.env.day + 1
    play.env.tape.H[future:] = 0.123456789
    play.env.tape.demand[future:] = 987654321
    assert play.status() == before
    text = json.dumps(before)
    for forbidden in (
        "truth",
        "events",
        "tape",
        "_event",
        "resolve_day",
        "salt",
        "secret",
        "fact_id",
        "source_ids",
        "subject_alias",
        "synthetic_fill",
        "987654321",
    ):
        assert forbidden not in text
    for site in play.env.world.nodes[12:]:
        assert site["id"] not in text and site["subject_alias"] not in text


def test_task_tools_refusals_approval_and_finish_use_cala_regime() -> None:
    async def run() -> None:
        task = get_task("supplygym-cala@1")
        assert isinstance(task, SupplyGymCalaTask)
        await task.reset(1000, salt="abc")
        assert task._play is not None and task._play.regime == CALA_REGIME
        assert {tool.name for tool in task.tools()} == {
            "get_status",
            "get_briefing",
            "set_controls",
            "finish",
        }
        briefing = await task.step(ToolCall(name="get_briefing"))
        assert briefing.ok and isinstance(briefing.content, dict)
        aliases = briefing.content["supplier_aliases"]
        assert isinstance(aliases, dict) and len(aliases) == 12
        assert briefing.content["receipt_cycle_days"] == 4
        assert briefing.content["regular_receipt_slots"] == {
            node["id"]: node["receipt_slot"] for node in task._play.env.world.nodes[:12]
        }
        before = await task.step(ToolCall(name="get_status"))
        bad: list[dict[str, Any]] = [
            {"approve_alternative": "A02"},
            {"supplier_weights": {"SITE-012": 1.0}},
            {"order_multiplier": 0.5},
        ]
        for args in bad:
            assert not (await task.step(ToolCall(name="set_controls", args=args))).ok
        assert await task.step(ToolCall(name="get_status")) == before
        result = await task.step(ToolCall(name="set_controls", args={"approve_alternative": "A01"}))
        assert result.ok
        alt = task._play.status()["suppliers"][10]
        assert alt["state"] == "onboarding" and alt["onboarding_days_left"] == 7
        assert not (
            await task.step(ToolCall(name="set_controls", args={"approve_alternative": "A01"}))
        ).ok
        assert (await task.step(ToolCall(name="finish"))).ok
        assert task.done() and not (await task.step(ToolCall(name="get_status"))).ok
        score = await task.grade()
        assert 0 <= score.total <= 1

    asyncio.run(run())


def test_weekly_ledger_matches_daily_simulation_and_does_not_change_ppo() -> None:
    play = Play(1001, CALA_REGIME)
    raw = Play(1001, CALA_REGIME)
    reference = make_decider(CALA_REFERENCE)
    for _ in range(6):
        status = play.status()
        assert reference.decide(status) == reference.decide(
            {key: value for key, value in status.items() if key != "last_week"}
        )
        assert np.max(np.abs(to_vector(StatusView.model_validate(status)) - play.env._obs())) < 2e-5
        play.apply({})
        raw.env.step(np.zeros(ACT_DIM))
        assert play.env.summary() == raw.env.summary()
        ledger = play.status()["last_week"]
        assert ledger["delta_vs_default"] == 0
        accounted = ledger["revenue"] - sum(
            ledger[key]
            for key in ("supplier_cost", "expedite", "holding", "lost_sales", "switching")
        )
        assert accounted == pytest.approx(ledger["profit"], abs=0.04)


def test_ppo_tool_path_matches_same_game_reference_replay() -> None:
    async def run() -> None:
        task = SupplyGymCalaTask()
        await task.reset(1002, salt="parity")
        decider = make_decider(CALA_REFERENCE)
        assert task._play is not None
        while not task.done():
            status = await task.step(ToolCall(name="get_status"))
            assert status.ok and isinstance(status.content, dict)
            args = decider.decide(status.content)
            assert (await task.step(ToolCall(name="set_controls", args=args))).ok
        replay = replay_reference(Play(1002, CALA_REGIME, "parity"))
        assert task._play.env.summary() == replay.env.summary()
        assert (await task.grade()).breakdown["profit"] == replay.env.summary()["profit"]
        assert grade_play(replay).breakdown["delta_vs_ref"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("salt", ["", "private-evaluation-salt"])
@pytest.mark.parametrize("policy_name", ["reference", "stop_early", "transfer_ppo"])
def test_registered_solution_and_prefix_match_same_tape_replay(salt: str, policy_name: str) -> None:
    """Harbor selects this registered reference too; neither path receives the secret salt."""

    async def run() -> None:
        task = SupplyGymCalaTask()
        obs = await task.reset(1004, salt=salt)
        policy = get_policy(policy_name, task.spec.ref)
        await policy.reset(task.spec, task.tools(), task.instruction())
        while not task.done():
            call = await policy.act(obs)
            assert call is not None
            result = await task.step(call)
            assert result.ok
            obs = Observation(last_result=result)
        assert task._play is not None
        expected = Play(1004, CALA_REGIME, salt)
        name = "ppo" if policy_name == "transfer_ppo" else CALA_REFERENCE
        if policy_name == "stop_early":
            decider = make_decider(name)
            for _ in range(4):
                expected.apply(decider.decide(expected.status()))
            expected.finished = True
        else:
            expected = replay_reference(expected, name)
        expected_score = grade_play(expected)
        score = await task.grade()
        assert score == expected_score
        assert task._play.env.summary() == expected.env.summary()
        if policy_name == "reference":
            assert score.breakdown["delta_vs_ref"] == 0
        if policy_name == "stop_early":
            assert task._play.weeks == 4

    asyncio.run(run())


def test_cala_identity_binds_checkpoint_bytes_and_policy_config(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crucible.envs.supplygym.policies import reference

    original = cala_scoring_hash()
    assert SupplyGymCalaTask.scoring_hash == original
    identity = cala_policy_identity()
    raw = (
        files("crucible.envs.supplygym.policies") / "data" / reference.CALA_CHECKPOINT
    ).read_bytes()
    assert identity["checkpoint_sha256"] == hashlib.sha256(raw).hexdigest()
    data = tmp_path / "data"
    data.mkdir()
    checkpoint = data / reference.CALA_CHECKPOINT
    checkpoint.write_bytes(raw)
    monkeypatch.setattr(reference, "files", lambda _: tmp_path)
    assert cala_scoring_hash() == original
    checkpoint.write_bytes(raw + b"identity-probe")
    assert cala_scoring_hash() != original
    checkpoint.write_bytes(raw)
    monkeypatch.setitem(reference.CONTROL_SCALES, "alternative_approval_threshold", 0.6)
    assert cala_scoring_hash() != original
    assert SupplyGymTask.scoring_hash == SupplyGymFreshTask.scoring_hash == SCORING_HASH


def test_training_record_matches_packaged_cala_reference_and_uses_only_train_seeds() -> None:
    record = json.loads(
        files("crucible.envs.supplygym.policies")
        .joinpath("data/cala_ppo.training.json")
        .read_bytes()
    )
    assert record["model_sha256"] == cala_policy_identity()["checkpoint_sha256"]
    assert record["world"] == cala_world_identity()
    tuning, selection, distillation = (
        record[key]
        for key in ("teacher_tuning_seeds", "checkpoint_validation_seeds", "distillation_seeds")
    )
    reserved = {*tuning, *selection, *distillation}
    assert len(reserved) == len(tuning) + len(selection) + len(distillation)
    assert reserved <= set(range(20000, 22000))
    rollout_bank = sorted(set(range(20000, 22000)) - reserved)
    assert (
        record["ppo_seed_partition"]["sha256_json"]
        == hashlib.sha256(json.dumps(rollout_bank).encode()).hexdigest()
    )
    assert 0 < record["checkpoint_ppo_transitions"] <= record["ppo_transitions"]


@pytest.mark.parametrize("task_ref", ["supplygym@1", "supplygym-fresh@1"])
def test_legacy_references_and_prefix_still_use_original_checkpoint(task_ref: str) -> None:
    async def run() -> None:
        task = get_task(task_ref)
        await task.reset(1001, salt="legacy-parity")
        assert isinstance(task, SupplyGymTask) and task._play is not None
        status = await task.step(ToolCall(name="get_status"))
        expected = make_decider("ppo").decide(task._play.status())
        for name in ("reference", "stop_early"):
            policy = get_policy(name, task_ref)
            await policy.reset(task.spec, task.tools(), task.instruction())
            assert await policy.act(Observation()) == ToolCall(name="get_status")
            call = await policy.act(Observation(last_result=status))
            assert call is not None and call.args == expected
        implicit = replay_reference(task._play)
        explicit = replay_reference(task._play, "ppo")
        assert implicit.env.summary() == explicit.env.summary()
        assert task.scoring_hash == SCORING_HASH

    asyncio.run(run())


@pytest.mark.parametrize("supplier", range(12))
def test_regular_receipt_uses_fixed_four_day_supplier_slot(supplier: int) -> None:
    env = Play(1000, CALA_REGIME).env
    assert ASSUMPTIONS["receipt_cycle_days"] == 4
    slot = env.world.nodes[supplier]["receipt_slot"]
    for nominal in range(4, 12):
        for delay in (0, 1, 5):
            booked, actual = env.regular_receipt(supplier, nominal, delay)
            assert 0 <= booked - nominal <= 3
            assert booked % 4 == slot
            assert 0 <= actual - (nominal + delay) <= 3
            assert actual % 4 == slot
            assert actual >= booked


def test_expedited_orders_bypass_regular_receipt_slot() -> None:
    env = Play(1000, CALA_REGIME).env
    assert env.tape is not None
    env.tape.H[:] = 1.0
    env.day = 1
    env.onhand = 0
    action = np.zeros(ACT_DIM)
    action[14] = 1
    costs: dict[str, Any] = {"purchase": 0.0, "expedite": 0.0, "_orders": []}
    env._place_orders(action, costs)
    expedited = [order for order in env.orders if order["ordered"] == 0 and order["qty"] > 0]
    regular = [order for order in env.orders if order["ordered"] > 0]
    assert expedited and regular
    for order in expedited:
        assert order["arr"] == order["nom"] == env.day + math.ceil(env.lead[order["sup"]] / 2)
    assert any(
        order["arr"] % 4 != env.world.nodes[order["sup"]]["receipt_slot"] for order in expedited
    )
    for order in regular:
        assert order["arr"] % 4 == env.world.nodes[order["sup"]]["receipt_slot"]


def test_status_arrival_is_booked_eta_and_late_is_zero_not_hidden_arrival() -> None:
    play = Play(1000, CALA_REGIME)
    play.env.day = 10
    order = {"sup": 0, "nom": 14, "arr": 25, "rev": 7, "qty": 12, "ordered": 12}
    play.env.orders = [order]
    assert play.status()["suppliers"][0]["days_to_next_arrival"] == 4
    order["arr"] = 100
    assert play.status()["suppliers"][0]["days_to_next_arrival"] == 4
    order["nom"] = 8
    assert play.status()["suppliers"][0]["days_to_next_arrival"] == 0
    assert (
        np.max(np.abs(to_vector(StatusView.model_validate(play.status())) - play.env._obs())) < 2e-5
    )
