"""持久化层：JSON 文件存储，服务重启后记录仍在。"""
import json
import os
import threading


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "members": {},
            "bookings": {},
            "nights": {},
            "counters": {"member": 0, "booking": 0, "seq": 0},
        }

    def save(self):
        """原子写入，避免写一半损坏数据。"""
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def next_id(self, kind, prefix):
        self.data["counters"][kind] += 1
        return "%s%04d" % (prefix, self.data["counters"][kind])

    def next_seq(self):
        """全局递增序号，用于候补排队先后。"""
        self.data["counters"]["seq"] += 1
        return self.data["counters"]["seq"]
