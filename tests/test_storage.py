import threading

from backend.storage.sqlite_repo import SQLiteRepository


def test_device_roundtrip(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    repo.upsert_device("arm_01", {"device_id": "arm_01", "ts": 100, "hw": "x"})
    got = repo.get_device("arm_01")
    assert got["hw"] == "x"
    assert got["last_seen"] == 100
    assert repo.get_device("missing") is None


def test_upsert_is_idempotent(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    repo.upsert_device("arm_01", {"device_id": "arm_01", "ts": 1})
    repo.upsert_device("arm_01", {"device_id": "arm_01", "ts": 2, "hw": "y"})
    assert len(repo.list_devices()) == 1
    assert repo.get_device("arm_01")["last_seen"] == 2


def test_tasks(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "t.db"))
    repo.add_task("t1", {"created_at": 5, "text": "a"})
    repo.add_task("t2", {"created_at": 1, "text": "b"})
    tasks = repo.list_tasks()
    assert [t["text"] for t in tasks] == ["b", "a"]  # ordered by created_at


def test_thread_safe_writes(tmp_path):
    repo = SQLiteRepository(str(tmp_path / "t.db"))

    def worker(n):
        for i in range(50):
            repo.upsert_device(f"dev_{n}_{i:02d}", {"device_id": f"d{n}", "ts": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(repo.list_devices()) == 200  # 4 workers * 50, no lost writes
