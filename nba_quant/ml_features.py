"""Numeric NBA features with explicit point-in-time and replay boundaries.

Public interfaces consume immutable dataset dictionaries returned by Store, not
``board.merged_history``: retaining revisions is necessary for as-of selection.
``build_training_rows`` returns nested numeric ``features`` and separate outcome
``targets``. ``rows_for_target`` exposes one ``label`` without other outcomes.
``features_for_game`` returns the identical feature schema without any labels.

Strict history requires completion < cutoff and original known_at <= cutoff.
Event-time replay deliberately ignores historical observation timing, preserves
the original timestamps, and is always ineligible for production promotion.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo

import numpy as np

from .schemas import utc
from .store import digest

VERSION = "nba-pit-features-0.1.0"
MODES = {"strict", "retrospective_event_time"}
EASTERN = ZoneInfo("America/New_York")
PLAYER_TARGETS = ("pts", "reb", "ast", "fg3m", "fg3a", "stl", "blk", "tov", "minutes", "fga", "fta", "pra")
TEAM_TARGETS = ("team_points", "opponent_points", "margin", "total", "win", "pace_proxy", "offensive_rating_proxy", "defensive_rating_proxy")
TARGET_REGISTRY = {
    **{key: {"scope": "player", "kind": "regression", "unit": "minutes" if key == "minutes" else "count",
             "availability": "canonical player box score", "conditional_population": "pregame roster in strict mode; observed box-score population in event-time replay"}
       for key in PLAYER_TARGETS},
    **{key: {"scope": "team", "kind": "binary" if key == "win" else "regression",
             "unit": "binary" if key == "win" else "points" if "proxy" not in key else "box_score_estimate",
             "availability": "final score" if "proxy" not in key else "requires FGA, FTA, OREB and TOV for both teams",
             "proxy": "0.44 FTA possession approximation; not measured play-by-play pace or ratings" if "proxy" in key else None}
       for key in TEAM_TARGETS},
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("NBA feature inputs must be finite numeric observations")
    return float(value)


def target_values(box):
    """Canonical player outcome values; missing observations are never zeros."""
    values = {key: _number(box[key]) for key in PLAYER_TARGETS if key not in {"reb", "pra"}}
    values["reb"] = _number(box["oreb"]) + _number(box["dreb"]) if "oreb" in box and "dreb" in box else _number(box["reb"])
    values["pra"] = values["pts"] + values["reb"] + values["ast"]
    if any(value < 0 for value in values.values()):
        raise ValueError("Player count/minute observations cannot be negative")
    return values


def _source_id(dataset):
    return dataset.get("id") or digest({"source": dataset.get("source"), "source_url": dataset.get("source_url"),
                                        "retrieved_at": str(dataset.get("retrieved_at")),
                                        "games": sorted(g["id"] for g in dataset.get("games", []))})


def _index(datasets, as_of, include_synthetic):
    datasets = [datasets] if isinstance(datasets, dict) else list(datasets)
    versions, rotations, audit = defaultdict(list), {}, Counter()
    source_ids = []
    for dataset in datasets:
        audit["input_datasets"] += 1
        if dataset.get("synthetic") is not False and not include_synthetic:
            audit["synthetic_datasets_skipped"] += 1
            continue
        if dataset.get("retrieved_at") and utc(dataset["retrieved_at"]) > as_of:
            audit["future_retrieval_datasets_skipped"] += 1
            continue
        sid = _source_id(dataset)
        observed_times = [utc(dataset[k]) for k in ("retrieved_at", "imported_at") if dataset.get(k)]
        source_timing = {"source_retrieved_at": dataset.get("retrieved_at"),
                         "source_imported_at": dataset.get("imported_at"),
                         "source_observed_at": max(observed_times) if observed_times else None}
        source_ids.append(sid)
        for game in dataset.get("games", []):
            audit["input_game_records"] += 1
            tip, completed, known = (utc(game[k]) for k in ("tipoff", "completed_at", "known_at"))
            if not tip < completed <= known:
                raise ValueError("Require original tipoff < completed_at <= known_at")
            wrapper = {"game": game, "source_id": sid, "synthetic": dataset.get("synthetic") is not False,
                       "tipoff": tip, "completed_at": completed, "known_at": known, **source_timing}
            existing = next((v for v in versions[game["id"]] if v["known_at"] == known), None)
            if existing and digest(existing["game"]) != digest(game):
                raise ValueError("Conflicting game revisions with identical known_at require resolution")
            if not existing:
                versions[game["id"]].append(wrapper)
            else:
                audit["duplicate_game_records_deduplicated"] += 1
                if wrapper["source_observed_at"] is not None and (existing["source_observed_at"] is None or wrapper["source_observed_at"] < existing["source_observed_at"]):
                    versions[game["id"]].remove(existing)
                    versions[game["id"]].append(wrapper)
        for rotation in dataset.get("rotations", []):
            stamp = utc(rotation["known_at"])
            if stamp <= as_of:
                item = {**rotation, "source_id": sid, "synthetic": dataset.get("synthetic") is not False, **source_timing}
                key = (rotation["player_id"], stamp)
                previous = rotations.get(key)
                if previous is not None:
                    fields = ("player_id", "team_id", "expected_minutes", "minutes_cap", "active_probability", "starter", "status")
                    if any(previous.get(field) != item.get(field) for field in fields):
                        raise ValueError("Conflicting rotation snapshots with identical known_at require resolution")
                if previous is None or (item["source_observed_at"] is not None and
                    (previous["source_observed_at"] is None or item["source_observed_at"] < previous["source_observed_at"])):
                    rotations[key] = item
    for records in versions.values():
        records.sort(key=lambda record: (record["known_at"], record["source_id"]))
    audit["unique_game_ids"] = len(versions)
    audit["retained_game_revisions"] = sum(len(records) for records in versions.values())
    audit["rotation_snapshots"] = len(rotations)
    return {"versions": versions, "rotations": list(rotations.values()), "audit": dict(audit), "source_ids": sorted(set(source_ids))}


def _visible(index, cutoff, mode, as_of):
    selected, late = [], 0
    for records in index["versions"].values():
        compatible = [record for record in records if record["completed_at"] < cutoff and record["known_at"] <= as_of
                      and (mode != "strict" or record["known_at"] <= cutoff)]
        if compatible:
            record = compatible[-1]
            selected.append(record)
            late += record["known_at"] > cutoff
    selected.sort(key=lambda record: (record["tipoff"], record["game"]["id"]))
    return selected, late


def _team_result(game, team_id):
    own = game["home_score"] if team_id == game["home_id"] else game["away_score"]
    opp = game["away_score"] if team_id == game["home_id"] else game["home_score"]
    result = {"team_points": _number(own), "opponent_points": _number(opp), "margin": _number(own) - _number(opp),
              "total": _number(own) + _number(opp), "win": float(own > opp)}
    try:
        sides = [[b for b in game["boxes"] if b["team_id"] == tid] for tid in (game["home_id"], game["away_id"])]
        if not all(sides):
            return result
        poss = sum(sum(_number(b["fga"]) + .44 * _number(b["fta"]) - _number(b["oreb"]) + _number(b["tov"]) for b in side) for side in sides) / 2
        if poss > 0:
            result.update(pace_proxy=poss * 48 / (48 + 5 * game.get("overtime_periods", 0)),
                          offensive_rating_proxy=100 * own / poss, defensive_rating_proxy=100 * opp / poss)
    except KeyError:
        pass  # Missing opportunity inputs do not become measured possessions.
    return result


def _mean(values):
    return float(np.mean(values)) if values else 0.


def _put_mean(features, key, values):
    features[key] = _mean(values)
    features[key + "_missing"] = float(not values)


def _ratio(features, key, numerator, denominator):
    features[key] = numerator / denominator if denominator else 0.
    features[key + "_missing"] = float(not denominator)


def _team_features(history, team_id, opponent_id, tipoff, window):
    features = {"is_home": 0., "hours_before_tipoff": 0.}
    for prefix, tid in (("team", team_id), ("opponent", opponent_id)):
        sample = [r for r in history if tid in {r["game"]["home_id"], r["game"]["away_id"]}][-window:]
        values = [_team_result(r["game"], tid) for r in sample]
        features[prefix + "_history_games"] = float(len(sample))
        for target in TEAM_TARGETS:
            suffix = {"team_points": "points_mean", "opponent_points": "points_allowed_mean", "win": "win_rate"}.get(target, target + "_mean")
            _put_mean(features, f"{prefix}_{suffix}", [v[target] for v in values if target in v])
        _put_mean(features, prefix + "_points_recent3_mean", [v["team_points"] for v in values[-3:]])
        _put_mean(features, prefix + "_points_allowed_recent3_mean", [v["opponent_points"] for v in values[-3:]])
        rest = (tipoff - sample[-1]["completed_at"]).total_seconds() / 3600 if sample else 0.
        features[prefix + "_rest_hours"] = rest
        features[prefix + "_rest_hours_missing"] = float(not sample)
        features[prefix + "_short_rest_30h"] = float(bool(sample) and (tipoff - sample[-1]["tipoff"]).total_seconds() <= 30 * 3600)
    features["rest_hours_difference"] = features["team_rest_hours"] - features["opponent_rest_hours"]
    return features


def _player_features(sample, tipoff, rotation):
    values = [target_values(box) for _, box in sample]
    features = {"player_history_games": float(len(sample)),
                "player_positive_minute_games": float(sum(v["minutes"] > 0 for v in values))}
    total_minutes = sum(v["minutes"] for v in values)
    for target in PLAYER_TARGETS:
        observed = [v[target] for v in values]
        _put_mean(features, f"player_{target}_mean", observed)
        _put_mean(features, f"player_{target}_recent3_mean", observed[-3:])
        features[f"player_{target}_sd"] = float(np.std(observed)) if observed else 0.
        if target != "minutes":
            _ratio(features, f"player_{target}_per_minute", sum(observed), total_minutes)
    positive = [r for r, b in sample if b["minutes"] > 0]
    features["player_hours_since_last_positive_minutes"] = (tipoff - positive[-1]["completed_at"]).total_seconds() / 3600 if positive else 0.
    features["player_hours_since_last_positive_minutes_missing"] = float(not positive)
    features["player_minutes_previous_7d"] = sum(v["minutes"] for (r, _), v in zip(sample, values) if tipoff - r["tipoff"] <= timedelta(days=7))
    features["rotation_snapshot_missing"] = float(rotation is None)
    for field in ("expected_minutes", "minutes_cap", "active_probability"):
        value = rotation.get(field) if rotation else None
        features["rotation_" + field + "_missing"] = float(value is None)
        if value is not None:
            value = _number(value)
            if not 0 <= value <= (1 if field == "active_probability" else 48):
                raise ValueError("Observed rotation values must respect probability/minute bounds")
        features["rotation_" + field] = value if value is not None else 0.
    return features


def _config(mode, min_player_games, min_team_games, window, cutoff_hours):
    if mode not in MODES:
        raise ValueError("Mode must be strict or retrospective_event_time")
    for value in (min_player_games, min_team_games, window):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("History counts and window must be positive integers")
    if max(min_player_games, min_team_games) > window or _number(cutoff_hours) <= 0:
        raise ValueError("History minima must fit the window and cutoff horizon must be positive")


def _provenance(records, cutoff):
    unique = {r["game"]["id"]: r for r in records}
    sources = {r["source_id"]: {"retrieved_at": r.get("source_retrieved_at"), "imported_at": r.get("source_imported_at"),
        "observed_at": r["source_observed_at"].isoformat() if r.get("source_observed_at") else None} for r in unique.values()}
    return {"history_game_ids": sorted(unique),
        "history_original_known_at": {gid: r["game"]["known_at"] for gid, r in unique.items()},
        "feature_latest_known_at": max((r["known_at"].isoformat() for r in unique.values()), default=None),
        "feature_latest_completed_at": max((r["completed_at"].isoformat() for r in unique.values()), default=None),
        "source_dataset_ids": sorted(sources), "source_observations": sources,
        "history_known_after_cutoff": sum(r["known_at"] > cutoff for r in unique.values()),
        "retrospective_source_observation": any(r.get("source_observed_at") is not None and r["source_observed_at"] > cutoff for r in unique.values()),
        "source_observation_missing": any(r.get("source_observed_at") is None for r in unique.values())}


def _rows(index, game, cutoff, as_of, mode, min_player_games, min_team_games, window, roster=None, replay_boxes=None):
    tipoff = utc(game["tipoff"])
    home = game.get("home_id") or game["home"]["id"]
    away = game.get("away_id") or game["away"]["id"]
    if home == away or not cutoff < tipoff or cutoff > as_of:
        raise ValueError("Distinct teams and a pregame cutoff no later than as_of are required")
    history, late = _visible(index, cutoff, mode, as_of)
    history = [record for record in history if record["game"]["id"] != game["id"]]
    membership = {}
    for rotation in sorted(index["rotations"], key=lambda r: (utc(r["known_at"]), r["source_id"])):
        if utc(rotation["known_at"]) <= cutoff:
            membership[rotation["player_id"]] = rotation
    for player in roster or []:
        stamp = player.get("known_at") or (player.get("source") or {}).get("retrieved_at")
        if stamp and utc(stamp) <= cutoff:
            normalized = {**player, "player_id": player.get("player_id") or player.get("id"), "known_at": stamp,
                          "source_id": (player.get("source") or {}).get("sha256", "provided-roster"), "synthetic": False,
                          "source_retrieved_at": (player.get("source") or {}).get("retrieved_at"),
                          "source_imported_at": player.get("imported_at"),
                          "source_observed_at": max([utc(t) for t in (stamp, (player.get("source") or {}).get("retrieved_at"), player.get("imported_at")) if t])}
            prior = membership.get(normalized["player_id"])
            if prior is None or utc(normalized["known_at"]) >= utc(prior["known_at"]):
                membership[normalized["player_id"]] = normalized
    if mode == "retrospective_event_time" and replay_boxes is not None:
        population = {b["player_id"]: {"player_id": b["player_id"], "team_id": b["team_id"]} for b in replay_boxes}
    else:
        population = membership
    counts = Counter(visible_history_games=len(history), history_known_after_cutoff=late)
    rows = []
    for team_id, opponent in ((home, away), (away, home)):
        own = [r for r in history if team_id in {r["game"]["home_id"], r["game"]["away_id"]}]
        opposing = [r for r in history if opponent in {r["game"]["home_id"], r["game"]["away_id"]}]
        if min(len(own), len(opposing)) < min_team_games:
            counts["team_entities_insufficient_history"] += 1
            continue
        base = _team_features(history, team_id, opponent, tipoff, window)
        base.update(is_home=float(team_id == home), hours_before_tipoff=(tipoff - cutoff).total_seconds() / 3600)
        team_history = {r["game"]["id"]: r for r in own[-window:] + opposing[-window:]}
        provenance = _provenance(team_history.values(), cutoff)
        metadata = {"game_id": game["id"], "team_id": team_id, "opponent_id": opponent,
                    "tipoff": tipoff.isoformat(), "cutoff": cutoff.isoformat(), "point_in_time_mode": mode,
                    "production_eligible": mode == "strict" and not any(r["synthetic"] for r in team_history.values())
                        and not provenance["retrospective_source_observation"] and not provenance["source_observation_missing"],
                    "provenance": provenance}
        rows.append({**metadata, "scope": "team", "entity_id": team_id, "features": base})
        for pid, player in sorted(population.items()):
            if player.get("team_id") != team_id:
                continue
            sample = [(r, b) for r in history for b in r["game"]["boxes"] if b["player_id"] == pid and b["team_id"] == team_id][-window:]
            if sum(b["minutes"] > 0 for _, b in sample) < min_player_games:
                counts["player_entities_insufficient_history"] += 1
                continue
            rotation = membership.get(pid)
            if rotation and rotation.get("team_id") != team_id:
                rotation = None
            features = {**base, **_player_features(sample, tipoff, rotation)}
            prov = {**_provenance(list(team_history.values()) + [r for r, _ in sample], cutoff),
                    "player_history_game_ids": [r["game"]["id"] for r, _ in sample],
                    "player_history_original_known_at": {r["game"]["id"]: r["game"]["known_at"] for r, _ in sample},
                    "roster_known_at": str(rotation["known_at"]) if rotation else None,
                    "population_basis": "PREGAME_OBSERVED_ROSTER" if mode == "strict" else "RETROSPECTIVE_OBSERVED_BOX_POPULATION"}
            if rotation is not None:
                prov["roster_source_retrieved_at"] = rotation.get("source_retrieved_at")
                prov["roster_source_imported_at"] = rotation.get("source_imported_at")
                prov["retrospective_source_observation"] = prov["retrospective_source_observation"] or (
                    rotation.get("source_observed_at") is not None and rotation["source_observed_at"] > cutoff)
                prov["source_observation_missing"] = prov["source_observation_missing"] or rotation.get("source_observed_at") is None
            rows.append({**metadata, "scope": "player", "entity_id": pid, "player_id": pid,
                         "production_eligible": metadata["production_eligible"] and rotation is not None and not rotation.get("synthetic")
                             and not prov["retrospective_source_observation"] and not prov["source_observation_missing"],
                         "provenance": prov, "features": features})
    return rows, dict(counts)


def _bundle(rows, audit):
    features = {scope: sorted({key for row in rows if row["scope"] == scope for key in row["features"]}) for scope in ("player", "team")}
    if any(not _number(value) == value for row in rows for value in row["features"].values()):
        raise ValueError("Invalid generated numeric feature")
    return {"rows": rows, "audit": {"feature_version": VERSION, **audit, "output_rows": len(rows),
                                     "scope_rows": dict(Counter(row["scope"] for row in rows)),
                                     "production_eligible_rows": sum(row["production_eligible"] for row in rows),
                                     "source_observation_late_rows": sum(row["provenance"].get("retrospective_source_observation", False) for row in rows),
                                     "source_observation_missing_rows": sum(row["provenance"].get("source_observation_missing", False) for row in rows),
                                     "production_note": "Temporal feature eligibility is not source authentication or trained-model promotion approval"},
            "feature_names": features, "target_registry": TARGET_REGISTRY}


def build_training_rows(datasets, *, mode="strict", min_player_games=3, min_team_games=3, window=10,
                        cutoff_hours=1., as_of=None, include_synthetic=False):
    """Build labelled rows at tipoff minus one hour by default, without backdating.

    Player labels require pre-cutoff roster population in strict mode. Missing
    roster snapshots do not become inferred pregame membership from final boxes.
    Replay may use observed participants, always marked non-production eligible.
    """
    _config(mode, min_player_games, min_team_games, window, cutoff_hours)
    now = datetime.now(timezone.utc)
    as_of = utc(as_of) if as_of is not None else now
    if as_of > now:
        raise ValueError("Training as_of cannot be in the future")
    index = _index(datasets, as_of, include_synthetic)
    rows, audit = [], Counter(index["audit"])
    for records in sorted(index["versions"].values(), key=lambda rs: (rs[-1]["tipoff"], rs[-1]["game"]["id"])):
        observed = [record for record in records if record["known_at"] <= as_of and record["completed_at"] <= as_of]
        if not observed:
            audit["label_games_not_yet_observed"] += 1
            continue
        target = observed[-1]
        game = target["game"]
        cutoff = target["tipoff"] - timedelta(hours=cutoff_hours)
        feature_rows, rejected = _rows(index, game, cutoff, as_of, mode, min_player_games, min_team_games, window,
                                       replay_boxes=game["boxes"] if mode == "retrospective_event_time" else None)
        audit.update(rejected)
        audit["label_games_considered"] += 1
        if mode == "strict" and not any(r["scope"] == "player" for r in feature_rows):
            audit["label_games_without_eligible_pregame_player_population"] += 1
        boxes = {(b["player_id"], b["team_id"]): b for b in game["boxes"]}
        for row in feature_rows:
            if row["scope"] == "player":
                box = boxes.get((row["player_id"], row["team_id"]))
                if box is None:
                    audit["pregame_players_missing_outcome_row"] += 1
                    continue  # Missing player outcome is not an invented DNP zero.
                labels = target_values(box)
            else:
                labels = _team_result(game, row["team_id"])
            rows.append({**row, "targets": labels,
                         "production_eligible": row["production_eligible"] and not target["synthetic"],
                         "label_completed_at": game["completed_at"], "label_known_at": game["known_at"],
                         "completed_at": game["completed_at"], "outcome_known_at": game["known_at"],
                         "label_source_dataset_id": target["source_id"]})
    return _bundle(rows, {**dict(audit), "mode": mode, "as_of": as_of.isoformat(), "cutoff_hours": cutoff_hours,
                          "minimum_player_games": min_player_games, "minimum_team_games": min_team_games,
                          "window": window, "status": "READY" if rows else "NO_ELIGIBLE_ROWS",
                          "original_known_at_preserved": True,
                          "replay_is_production_eligible": False})


def features_for_game(datasets, game, *, cutoff=None, mode="strict", roster=None, min_player_games=3,
                      min_team_games=3, window=10, cutoff_hours=1., as_of=None, include_synthetic=False):
    """Inference rows with no target outcomes; missing roster/history stays absent.

    For an early forecast, the default cutoff is the earlier of actual as_of and
    tipoff-minus-one-hour; ``hours_before_tipoff`` exposes the actual horizon.
    Explicit cutoff must not follow actual as_of or reach tipoff.
    """
    _config(mode, min_player_games, min_team_games, window, cutoff_hours)
    now = datetime.now(timezone.utc)
    as_of = utc(as_of) if as_of is not None else now
    if as_of > now:
        raise ValueError("Feature as_of cannot be in the future")
    cutoff = utc(cutoff) if cutoff is not None else min(utc(game["tipoff"]) - timedelta(hours=cutoff_hours), as_of)
    index = _index(datasets, as_of, include_synthetic)
    rows, audit = _rows(index, game, cutoff, as_of, mode, min_player_games, min_team_games, window, roster=roster)
    return _bundle(rows, {**index["audit"], **audit, "mode": mode, "cutoff": cutoff.isoformat(),
                          "status": "READY" if rows else "NO_ELIGIBLE_ROWS", "original_known_at_preserved": True})


def rows_for_target(rows_or_bundle, target):
    """Return a list with one label, retaining only its target's declared scope."""
    if target not in TARGET_REGISTRY:
        raise ValueError("Unsupported NBA ML target")
    source = rows_or_bundle["rows"] if isinstance(rows_or_bundle, dict) else rows_or_bundle
    return [{**{key: value for key, value in row.items() if key not in {"targets", "label"}},
             "label": _number(row["targets"][target]), "target_name": target}
            for row in source if row["scope"] == TARGET_REGISTRY[target]["scope"] and target in row.get("targets", {})]


def chronological_splits(rows, fractions=(.6, .15, .1, .15), embargo_hours=0.):
    """Chronological game-grouped partitions with label-availability purging.

    Entire Eastern tipoff game-day slates stay together, including West Coast
    games crossing UTC midnight. Entire earlier game groups are purged
    unless completion and (strict mode) label knowledge precede the next fold's
    first cutoff, minus any embargo. Replay purges completion only and is clearly
    marked retrospective; it never changes original known_at timestamps.
    """
    if isinstance(rows, dict):
        rows = rows["rows"]
    if len(fractions) != 4 or any(not _number(v) > 0 for v in fractions) or not math.isclose(sum(fractions), 1, abs_tol=1e-9):
        raise ValueError("Four positive chronological fractions summing to one are required")
    if not _number(embargo_hours) >= 0:
        raise ValueError("Embargo must be nonnegative")
    names = ("train", "tune", "calibration", "test")
    by_game, audit = defaultdict(list), {"grouping": "game_id within America/New_York tipoff game-day cohorts",
        "fractions": list(fractions), "embargo_hours": embargo_hours, "purged_game_ids": {},
        "production_eligible": bool(rows) and all(row.get("production_eligible") for row in rows)}
    for row in rows:
        by_game[row["game_id"]].append(row)
    cohorts = defaultdict(list)
    for gid, group in by_game.items():
        cutoffs = {utc(row["cutoff"]) for row in group}
        if len(cutoffs) != 1:
            raise ValueError("One game cannot have multiple prediction cutoffs in a split")
        tipoffs = {utc(row["tipoff"]) for row in group}
        if len(tipoffs) != 1:
            raise ValueError("One game cannot have multiple tipoff timestamps in a split")
        cohorts[next(iter(tipoffs)).astimezone(EASTERN).date()].append(gid)
    times = sorted(cohorts)
    if len(times) < 4:
        return {**{name: [] for name in names}, "audit": {**audit, "status": "INSUFFICIENT_GROUPS",
                "input_games": len(by_game), "input_day_cohorts": len(times), "required_cohorts": 4,
                "production_eligible": False}}
    sizes, left = [], len(times)
    for i, fraction in enumerate(fractions[:-1]):
        size = max(1, min(left - (3 - i), int(len(times) * fraction)))
        sizes.append(size)
        left -= size
    sizes.append(left)
    partitions, cursor = {}, 0
    for name, size in zip(names, sizes):
        partitions[name] = [gid for stamp in times[cursor:cursor + size] for gid in sorted(cohorts[stamp])]
        cursor += size
    result = {}
    for i, name in enumerate(names):
        selected, purged = [], []
        boundary = min(utc(by_game[gid][0]["cutoff"]) for gid in partitions[names[i + 1]]) - timedelta(hours=embargo_hours) if i < 3 else None
        for gid in partitions[name]:
            group = by_game[gid]
            permitted = boundary is None or all(utc(row.get("label_completed_at", row.get("completed_at"))) < boundary
                and (row.get("point_in_time_mode") == "retrospective_event_time"
                     or utc(row.get("label_known_at", row.get("outcome_known_at"))) < boundary) for row in group)
            if permitted:
                selected.extend(group)
            else:
                purged.append(gid)
        result[name] = sorted(selected, key=lambda row: (utc(row["cutoff"]), row["game_id"], row["entity_id"]))
        audit["purged_game_ids"][name] = purged
    audit.update(status="READY" if all(result[name] for name in names) else "EMPTY_AFTER_PURGE",
                 input_games=len(by_game), input_day_cohorts=len(times),
                 input_cutoff_cohorts=len({utc(row["cutoff"]) for row in rows}),
                 rows={name: len(result[name]) for name in names},
                 games={name: len({row["game_id"] for row in result[name]}) for name in names},
                 knowledge_purge_ignored_for_replay=any(row.get("point_in_time_mode") == "retrospective_event_time" for row in rows))
    return {**result, "audit": audit}
