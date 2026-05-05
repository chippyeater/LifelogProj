# 任务

请把视频划分为按时间顺序排列的多个时间段，并输出每个时间段的行为目标、局部上下文和关键帧候选信息。

# 输入上下文

活动主题：{{ activity }}
人物：{{ people }}
时间：{{ time }}
地点：{{ location }}
视频总时长：{{ video_length }}

# 输出结构

只输出 JSON，对象顶层只包含 `timeline`。

每个时间段必须包含：
- `start_time`
- `end_time`
- `behavior_goal`
- `segment_description`
- `reason`
- `keyframes`

每个关键帧必须包含：
- `frame_time`
- `interaction_candidates`

每个候选只包含：
- `object_name`
- `action_name`

# 核心约束

1. 用“行为目标”作为切段的第一依据，不要按固定时长平均切段。
2. 行为目标是人物在一段连续时间内围绕同一个意图持续进行的一组相关操作，不是单个瞬时动作，不是单个物体名称，也不是宽泛活动总主题。
3. 行为目标必须服务于整体活动主题；不重要的局部行为不要单独切段，应并入前后最接近的有效时间段。
4. `segment_description` 用一句短句描述这一整段具体发生了什么，给后续阶段提供局部上下文。
5. `reason` 用简短自然语言短语说明切段依据，不要写长句。
6. `interaction_candidates` 只保留关键帧里与人物交互直接相关的候选对象和动作，不能为空。
7. 选择关键帧时，必须优先选择目标物体清晰可见、交互动作容易辨认的画面。

# 示例

```json
{
  "timeline": [
    {
      "start_time": "00:01:00.000",
      "end_time": "00:01:12.000",
      "behavior_goal": "挑选乳制品",
      "segment_description": "在乳制品区查看并挑选牛奶，最后拿起一瓶查看。",
      "reason": "围绕同一采购目标",
      "keyframes": [
        {
          "frame_time": "00:01:05.000",
          "interaction_candidates": [
            {
              "object_name": "牛奶",
              "action_name": "拿起查看"
            }
          ]
        }
      ]
    }
  ]
}
```
