from datetime import datetime, timezone
from uuid import uuid4

from .evaluation import score_forecast
from .model import MODEL_VERSION, prepare
from .simulation import run
from .store import digest


def get_dataset(store, key):
    dataset = next((d for d in store.datasets() if d["id"] == key), None)
    if dataset is None:
        raise ValueError("Unknown dataset")
    return dataset


def forecast(store, request):
    dataset = get_dataset(store, request.dataset_id)
    context = prepare(dataset, request)
    # This exact frozen snapshot, seed, version and source hash can be replayed.
    result = run(context, request.simulations, request.seed)
    payload = {"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
               "synthetic": dataset["synthetic"], "source": dataset["source"],
               "source_url": dataset["source_url"], "request": request.model_dump(mode="json"),
               "input_hash": digest({"dataset_id": dataset["id"], "request": request.model_dump(mode="json"),
                                     "feature_hash": context["feature_hash"], "version": MODEL_VERSION}),
               "model": {"version": MODEL_VERSION, **context}, "result": result}
    store.save_forecast(payload)
    return payload


def record_outcome(store, forecast_id, dataset_id, game_id):
    f = store.forecast(forecast_id)
    if f is None:
        raise ValueError("Unknown forecast")
    d = get_dataset(store, dataset_id)
    g = next((g for g in d["games"] if g["id"] == game_id), None)
    if g is None:
        raise ValueError("Unknown outcome game")
    result = score_forecast(f, d, g)
    store.record_outcome(forecast_id, result)
    return result
