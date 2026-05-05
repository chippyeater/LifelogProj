# 任务

请基于活动的时间结构和关键帧图片组，重建适合后续回忆任务使用的事件序列，并在同一轮里为每个事件筛出最终需要保留的实体。

# 输入

下面是活动的时间结构 JSON。系统还会同时提供这些 `frame_id` 对应的关键帧图片组，请结合 JSON 和图片一起完成事件重建与实体筛选：

```json
{{ stage1_json }}
```

# 输出结构

只输出 JSON，顶层只包含 `events`。

每个事件必须包含：
- `event_name`
- `event_description`
- `start_time`
- `end_time`
- `reason`
- `source_segment_indices`
- `representative_frame_ids`
- `selected_entities`

每个实体必须包含：
- `entity_name`
- `entity_type`
- `selection_reason`
- `evidence_frame_ids`
- `anchor_frame_id`

# 核心约束

1. 事件按“行为目标一致性”重建，不要默认把每个 `timeline` 时间段直接照抄成一个事件。
2. 只有当相邻时间段明显服务于同一个行为目标，并且事后会被自然回忆成同一件事时，才允许合并。
3. 如果行为目标已经切换，即使时间连续，也应拆成不同事件。
4. 所有 `event_name` 必须保持同一抽象层级，以行为目标为粒度，不要写成单个物体名，也不要写成过宽类别。
5. `event_description` 只描述支撑事件边界和行为目标的核心信息，不要提前写过细的视觉细节。
6. 每个事件都必须至少绑定一个来源时间段和一个代表帧。
7. `source_segment_indices` 使用从 0 开始的编号。
8. `reason` 用简短自然语言短语说明为什么这样重建事件，不要写成长段解释。
9. `selected_entities` 只保留真正适合后续回忆任务使用的最终实体，不要把背景物体、纯陈列物、辅助场所信息或持续重复但作用未变化的辅助容器反复保留。
10. 每个实体都必须能追溯到明确的证据帧，`anchor_frame_id` 必须是最适合作为后续细节生成主锚点的一帧。
11. `entity_type` 只允许 `OBJECT` 或 `PERSON`。

# 示例

```json
{
  "events": [
    {
      "event_name": "挑选饮品",
      "event_description": "在冷藏区查看并挑选饮品。",
      "start_time": "00:01:00.000",
      "end_time": "00:01:12.000",
      "reason": "同一行为目标",
      "source_segment_indices": [3],
      "representative_frame_ids": ["f_005"],
      "selected_entities": [
        {
          "entity_name": "牛奶",
          "entity_type": "OBJECT",
          "selection_reason": "与当前事件主行为直接相关",
          "evidence_frame_ids": ["f_005"],
          "anchor_frame_id": "f_005"
        }
      ]
    }
  ]
}
```
