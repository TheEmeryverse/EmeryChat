import json
import os
import sys
import tempfile
import importlib

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reload_memory_module(memory_store_path: str):
    os.environ["MEMORY_STORE_PATH"] = memory_store_path

    import emery.config as config
    import emery.memory as memory

    importlib.reload(config)
    importlib.reload(memory)
    return memory


def test_corrupt_store_is_repaired():
    print("--- 1. Testing corrupt memory store repair ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = os.path.join(tmpdir, "memory_store.json")
        with open(store_path, "w", encoding="utf-8") as handle:
            handle.write("")

        memory = _reload_memory_module(store_path)
        store = memory._load_store_locked()

        assert store["items"] == [], store
        assert store["next_id"] == 1, store

        with open(store_path, "r", encoding="utf-8") as handle:
            repaired = json.load(handle)
        assert repaired["items"] == [], repaired
        assert os.path.exists(store_path + ".corrupt"), "Expected corrupt backup file"
    print("✅ Corrupt memory store repair verified.")


def test_save_user_memory_rewrites_valid_json():
    print("--- 2. Testing memory save after repair ---")
    with tempfile.TemporaryDirectory() as tmpdir:
        store_path = os.path.join(tmpdir, "memory_store.json")
        with open(store_path, "w", encoding="utf-8") as handle:
            handle.write("")

        memory = _reload_memory_module(store_path)

        store = memory._load_store_locked()
        item, created = memory._store_item_locked(
            store,
            owner_user_id=123,
            source_user_id=123,
            source_chat_id=123,
            item_type="fact",
            text="Hudson likes reliable weather aliases.",
            scope="private",
            visibility="dm_only",
        )
        assert created is True, item
        memory._save_store_locked(store)

        with open(store_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        assert len(payload["items"]) == 1, payload
        assert payload["items"][0]["text"] == "Hudson likes reliable weather aliases.", payload
    print("✅ Memory save after repair verified.")


def main():
    test_corrupt_store_is_repaired()
    test_save_user_memory_rewrites_valid_json()
    print("\n🎉 Memory store verified successfully!")


if __name__ == "__main__":
    main()
