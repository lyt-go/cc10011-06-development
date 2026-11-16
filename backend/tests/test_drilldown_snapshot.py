"""多维下钻路径版本快照 + 标准维度映射的回归测试。

运行：backend/.venv/bin/python -m unittest discover -s backend/tests -v
不依赖 pytest / httpx，直接调用服务层与数据仓库，保证克隆下来就能跑。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.dimensionmap import DimensionmapService  # noqa: E402
from app.services.drilldown import DrilldownService, drilldown_snapshot_service  # noqa: E402
from app.services.standardmapping import standard_mapping_service  # noqa: E402
from app.store import store  # noqa: E402


class SnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        drilldown_snapshot_service._cache = {}
        standard_mapping_service.sync_all()

    def tearDown(self) -> None:
        drilldown_snapshot_service._cache = {}

    # ---------- 标准维度映射落库 ----------

    def test_sync_creates_one_standard_per_source_with_version(self) -> None:
        standards = standard_mapping_service.list_standards()
        self.assertEqual([row["源维度"] for row in standards], ["category", "channel", "city", "province"])
        for row in standards:
            self.assertIsNotNone(row["版本ID"], f"{row['源维度']} 缺少版本")
            self.assertTrue(row["覆盖模块"])

    def test_sync_is_idempotent_and_versions_do_not_duplicate(self) -> None:
        before = len(standard_mapping_service.list_versions())
        summary = standard_mapping_service.sync_all()
        self.assertEqual(summary["新增版本数"], 0)
        self.assertEqual(len(standard_mapping_service.list_versions()), before)

    def test_conflict_picks_single_traceable_standard_with_tradeoff(self) -> None:
        province = standard_mapping_service.current_standard("province")
        self.assertEqual(province["规则编号"], "DIME-0008")
        self.assertTrue(province["冲突"])
        self.assertIn("权衡代价", province["权衡说明"])
        versions = standard_mapping_service.list_versions("province")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["口径状态"], "已生效")
        self.assertEqual(len(versions[0]["候选规则"]), 2)

    def test_module_writes_sync_references_without_new_version(self) -> None:
        before = len(standard_mapping_service.list_versions())
        entry, _ = DrilldownService().create_entry({
            "路径编号": "DRIL-9001",
            "起始维度": "channel",
            "下钻层级序列": "channel > category",
        })
        self.assertIsNotNone(entry)
        channel = standard_mapping_service.current_standard("channel")
        self.assertIn("drilldown", channel["覆盖模块"])
        self.assertEqual(len(standard_mapping_service.list_versions()), before)

    # ---------- 上游校验与受理顺序 ----------

    def test_rejects_when_path_not_enabled(self) -> None:
        snapshot, message = drilldown_snapshot_service.submit(2, {})
        self.assertIsNone(snapshot)
        self.assertIn("路径评审中", message)
        self.assertIn("不能生成分层聚合明细数据集", message)

    def test_rejects_when_layer_has_no_standard_mapping(self) -> None:
        snapshot, message = drilldown_snapshot_service.submit(4, {})
        self.assertIsNone(snapshot)
        self.assertIn("region", message)
        ledger = store.rows("drilldown_generation_ledger")[-1]
        self.assertFalse(ledger["受理"])
        self.assertIsNone(ledger["受理序号"])

    def test_rejects_when_start_dimension_disagrees_with_sequence(self) -> None:
        service = DrilldownService()
        entry, _ = service.create_entry({
            "路径编号": "DRIL-9002",
            "起始维度": "city",
            "下钻层级序列": "province > city",
            "默认度量": "订单量",
            "路径深度": "2",
        })
        service.run_action(entry["id"], "提交路径评审")
        service.run_action(entry["id"], "启用路径")
        snapshot, message = drilldown_snapshot_service.submit(entry["id"], {})
        self.assertIsNone(snapshot)
        self.assertIn("起始维度", message)

    def test_first_passing_submission_wins_and_duplicate_merges(self) -> None:
        first, first_message = drilldown_snapshot_service.submit(3, {"提交人": "甲"})
        self.assertIsNotNone(first, first_message)
        second, second_message = drilldown_snapshot_service.submit(3, {"提交人": "乙"})
        self.assertEqual(second["id"], first["id"])
        self.assertIn("已合并", second_message)
        snapshots, total = drilldown_snapshot_service.list_snapshots()
        self.assertEqual(total, 1)
        ledger, _ = drilldown_snapshot_service.list_ledger(path_id=3)
        accepted = [row for row in ledger if row["受理"]]
        merged = [row for row in ledger if row["受理结果"] == "已合并"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["受理序号"], 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["提交人"], "乙")

    def test_concurrent_submissions_only_accept_one(self) -> None:
        results: list[tuple[int, str]] = []
        lock = threading.Lock()

        def submit(index: int) -> None:
            snapshot, message = drilldown_snapshot_service.submit(3, {"提交人": f"并发{index}"})
            with lock:
                results.append((index, message))

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 6)
        snapshots, total = drilldown_snapshot_service.list_snapshots(path_id=3)
        self.assertEqual(total, 1)
        accepted_ledger = [row for row in store.rows("drilldown_generation_ledger") if row["受理"]]
        self.assertEqual(len(accepted_ledger), 1)
        self.assertEqual(accepted_ledger[0]["受理序号"], 1)

    # ---------- 分层聚合与可追溯 ----------

    def test_snapshot_layers_bind_one_version_each(self) -> None:
        snapshot, message = drilldown_snapshot_service.submit(3, {})
        self.assertIsNotNone(snapshot, message)
        self.assertEqual(snapshot["层级序列"], ["province", "city", "channel"])
        self.assertEqual(len(snapshot["版本绑定"]), 3)
        version_ids = [binding["标准维度版本ID"] for binding in snapshot["版本绑定"]]
        self.assertTrue(all(version_ids))
        self.assertEqual(len(set(version_ids)), 3)
        for layer in snapshot["层汇总"]:
            self.assertTrue(layer["数据截止时间"])
            self.assertTrue(layer["口径指纹"])

    def test_layer_totals_are_consistent_across_depths(self) -> None:
        snapshot, _ = drilldown_snapshot_service.submit(3, {})
        totals = {layer["层级序号"]: layer["度量小计"] for layer in snapshot["层汇总"]}
        self.assertEqual(set(totals.values()), {975})
        self.assertEqual(sum(layer["明细行数"] for layer in snapshot["层汇总"]), snapshot["明细行数"])

    def test_query_supports_start_dimension_level_locate_and_sort(self) -> None:
        snapshot, _ = drilldown_snapshot_service.submit(3, {})
        ordered = drilldown_snapshot_service.query_layers(
            snapshot["id"], sort_field="度量值", sort_order="desc"
        )
        self.assertEqual(ordered["items"][0]["维度路径"], "province=广东")
        located = drilldown_snapshot_service.query_layers(snapshot["id"], locate="深圳")
        self.assertEqual(located["定位层级"], 2)
        # 同一分组值会沿层级展开：第 2 层定位到城市，第 3 层继续展开渠道。
        self.assertEqual({row["层级序号"] for row in located["items"]}, {2, 3})
        self.assertTrue(all("深圳" in row["维度路径"] for row in located["items"]))
        self.assertEqual(
            [row["层级序号"] for row in located["items"]],
            sorted(row["层级序号"] for row in located["items"]),
        )
        mismatch = drilldown_snapshot_service.query_layers(snapshot["id"], start_dimension="channel")
        self.assertIn("起始维度", mismatch["error"])
        by_level = drilldown_snapshot_service.query_layers(snapshot["id"], level=1)
        self.assertTrue(all(row["层级序号"] == 1 for row in by_level["items"]))

    # ---------- 口径漂移 ----------

    def test_standard_change_flags_drift_and_cache_keeps_old_version(self) -> None:
        snapshot, _ = drilldown_snapshot_service.submit(3, {})
        first_layer = snapshot["版本绑定"][0]
        DimensionmapService().run_action(2, "生效规则")
        current = standard_mapping_service.current_standard("province")
        self.assertNotEqual(current["版本ID"], first_layer["标准维度版本ID"])

        check = drilldown_snapshot_service.check_snapshot(snapshot["id"])
        self.assertTrue(check["口径漂移"])
        statuses = {row["层级序号"]: row["状态"] for row in check["层级核对"]}
        self.assertEqual(statuses[1], "口径已变更")
        self.assertEqual(statuses[2], "一致")

        cached = drilldown_snapshot_service.cached_drill(3)
        self.assertTrue(cached["缓存命中"])
        self.assertEqual(cached["快照编号"], snapshot["快照编号"])
        self.assertIn("旧维度", cached["提示"])
        cached_versions = [item["标准维度版本编号"] for item in cached["版本绑定"]]
        self.assertEqual(cached_versions[0], first_layer["标准维度版本编号"])

        regenerated, _ = drilldown_snapshot_service.submit(3, {"提交人": "重算"})
        self.assertNotEqual(regenerated["id"], snapshot["id"])
        self.assertEqual(
            regenerated["版本绑定"][0]["标准维度版本ID"], current["版本ID"]
        )
        self.assertEqual(regenerated["版本序号"], 2)

    def test_path_config_change_is_detected(self) -> None:
        snapshot, _ = drilldown_snapshot_service.submit(3, {})
        path = store.find("drilldown", 3)
        path["下钻层级序列"] = "province > city"
        path["路径深度"] = "2"
        check = drilldown_snapshot_service.check_snapshot(snapshot["id"])
        self.assertTrue(check["路径配置已变更"])
        self.assertIn("层级序列已变更", check["结论"])
        self.assertEqual(snapshot["层级序列"], ["province", "city", "channel"])

    def test_missing_standard_mapping_is_reported_globally(self) -> None:
        missing = standard_mapping_service.missing_dimensions()
        self.assertEqual([item["源维度"] for item in missing], ["region"])


if __name__ == "__main__":
    unittest.main()
