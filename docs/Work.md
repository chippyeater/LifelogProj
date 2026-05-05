# Work

## 1. 当前后端真实入口

当前完整视频处理入口不是 HTTP 接口，而是本地 CLI：

```bash
python run_pipeline.py --user 001 --video 001.mp4 --pipeline-mode full_context
```

真实入口代码在：

- [run_pipeline.py](e:/VSCodeProjects/LifelogProj/run_pipeline.py)

当前 `api_server.py` 只提供读取型接口：

- `/api/job-status`
- `/api/context`
- `/api/game-meta`
- `/api/game-flow`
- `/assets/<user>/<path>`
- `/api/tts`

也就是说：

- 现在“处理视频”靠脚本
- 现在“给 Unity/网页端取结果”靠 HTTP

## 2. HTTP 任务接口

已新增：

```http
POST /api/tasks/process
```

当前行为：

1. 把上传视频保存到 `videos/{video_name}`
2. 写入数据库记录
3. 生成 `task_id` / `job_id`
4. 启动后台子进程执行：

```bash
python run_pipeline.py --user {user} --video {video_name} --pipeline-mode full_context
```

5. 立即返回 `job_id`

当前接口形态是 `multipart/form-data`，支持：

- `video`
- `user`
- `activity`
- `people`
- `time`
- `location`
- `shopping_list`

说明：

- 目前 `shopping_list` 只接收并写入简单状态文件，未进入 `run_pipeline.py` 主处理逻辑
- 后端内部仍然复用现有 `run_pipeline.py`

## 3. 网页端是否可以只围绕三个文件工作

可以，而且这是当前最合适的最小方案。

网页端目前完全可以只围绕这三个产物工作：

- `output/{user}/extracted_context.json`
- `output/{user}/GameMeta.json`
- `output/{user}/GameFlow.json`

理由：

1. `full_context` 是当前主链路
2. `GameMeta.json` / `GameFlow.json` 是 Unity 最终消费结果
3. `extracted_context.json` 是医生/护工网页端最适合查看和审核的中间语义结果
4. `staged cache` 现在更像开发调试资产，不适合先暴露给业务网页端

所以网页端第一版完全可以：

- 不接 `pipeline_cache`
- 不做复杂分阶段恢复
- 只展示和管理最终三份文件

## 4. /api/job-status 现在能不能给网页端用

能用，而且已经补了网页端友好字段。

当前 `/api/job-status` 已经能返回：

- `status`
- `progress`
- `ready`
- `pipeline_state`
- `error`
- `game_meta_url`
- `game_flow_url`
- `extracted_context_url`

它当前更偏“Unity 轮询接口”，不是“网页任务面板接口”。

当前会额外返回：

- `task_id`
- `current_step`

当前返回示例结构：

```json
{
  "user": "001",
  "task_id": "task_001_xxx",
  "status": "processing",
  "ready": false,
  "progress": 40,
  "current_step": "正在分析视频",
  "error": null
}
```

说明：

- Unity 仍然可以继续使用老字段
- 网页端可以直接用新字段

## 5. 简单状态文件

当前已引入简单状态文件方案。

建议增加一个简单状态文件，例如：

```json
{
  "status": "processing",
  "current_step": "正在分析视频",
  "progress": 40,
  "error": null
}
```

当前路径：

- `output/{user}/status.json`

当前是最小实现，只按 `user` 维护一份。

后续如果要支持同一用户多任务并行，建议升级为：

- `output/{user}/{video_name}.status.json`

或者数据库持久化任务表。

## 6. Unity 现在请求的是哪个接口

Unity 当前主要请求的是：

1. `/api/job-status`
2. `/api/game-meta`
3. `/api/game-flow`
4. `/assets/{user}/{relative_path}`

具体逻辑在：

- [LoginRemoteLoader.cs](F:/Unity/Projects/YTY/YTY/YTY/Assets/Scripts/Scene0/LoginRemoteLoader.cs)

当前链路是：

1. 先轮询 `/api/job-status`
2. `ready == true` 后再下载：
   - `/api/game-meta?user=...`
   - `/api/game-flow?user=...`
3. 再根据 `GameFlow.json` 里的相对路径去请求：
   - `/assets/{user}/frames/...`
   - `/assets/{user}/option_images/...`
   - `/assets/{user}/media/audio/...`
   - `/assets/{user}/media/video/...`

所以答案是：

- Unity 不只是请求 `/api/game-meta` 和 `/api/game-flow`
- Unity 的真正入口是 `/api/job-status`

## 7. 网页端改造时对 Unity 的兼容要求

网页端接入后，需要保证 Unity 现有逻辑不变。

这意味着以下接口必须继续保留：

- `/api/job-status`
- `/api/game-meta`
- `/api/game-flow`
- `/assets/{user}/{path}`

推荐做法不是改 Unity 接口，而是：

1. 新增 `POST /api/tasks/process`
2. 后台继续跑 `run_pipeline.py`
3. 继续产出：
   - `output/{user}/extracted_context.json`
   - `output/{user}/GameMeta.json`
   - `output/{user}/GameFlow.json`
4. `/api/job-status` 兼容旧字段，同时可读更简单的人类状态

也就是说：

- Unity 保持不动
- 网页端复用同一套产物和查询接口

## 8. 当前网页端第一版范围

当前建议范围不变：

1. 通过 `POST /api/tasks/process` 启动任务
2. 通过 `/api/job-status` 轮询状态
3. 只围绕这三个结果文件工作

- `extracted_context.json`
- `GameMeta.json`
- `GameFlow.json`

4. 暂不接入 staged cache
5. 继续复用 Unity 现有接口

## 9. 下一步实现建议

下一步如果开始做代码，建议顺序是：

1. 新增 `POST /api/tasks/process`
2. 增加后台子进程/任务执行器，负责调用 `run_pipeline.py`
3. 增加简单任务状态写入
4. 调整 `/api/job-status`，兼容输出：
   - 旧字段给 Unity
   - 新字段给网页端
5. 最后再做网页端页面


二、AI 调用相关代码定位

公共层

AIInteractionManager.cs (line 22) systemPromptTemplate
AIInteractionManager.cs (line 38) RequestGuidance
AIInteractionManager.cs (line 44) ProcessRequestQueue
AIInteractionManager.cs (line 62) BuildFullPrompt
说明：

从职责上看，这里最适合成为 AI 调用总入口。

但它现在只有一个很底的 RequestGuidance(userInput, componentRules)，还缺“场景语义层”的 API。

CallAIManager.cs (line 33) TriggerRequest

CallAIManager.cs (line 49) CallAIManagerWithDelay

CallAIManager.cs (line 78) GetLatestUserInput

CallAIManager.cs (line 111) GetDynamicComponentRules

CallAIManagerButton.cs (line 33) TriggerRequest

CallAIManagerButton.cs (line 49) CallAIManagerWithDelay

CallAIManagerButton.cs (line 78) GetLatestUserInput

CallAIManagerButton.cs (line 111) GetDynamicComponentRules

问题：

CallAIManager 和 CallAIManagerButton 基本是重复代码。

这层应该只保留一个极薄的触发器，核心逻辑往 AIInteractionManager 合并。

QwenApiClient.cs (line 21) SendMessageToQwen

QwenApiClient.cs (line 44) PostRequestAsync

说明：

这是 HTTP 客户端层，不建议塞场景策略。
Scene1

ClickFunction.cs (line 104) 正确后 CallAIManager.Instance.TriggerRequest(...)
ClickFunction.cs (line 149) 直接 AIInteractionManager.Instance.RequestGuidance(...)
问题：

同一个场景里同时存在“经 CallAIManager”与“直接 RequestGuidance”两种入口。
Scene2

RequestHintButton.cs (line 123) Scene1 hint 文案
RequestHintButton.cs (line 128) Scene1 最终揭示
RequestHintButton.cs (line 139) Scene2 recall hint 文案
RequestHintButton.cs (line 160) Scene2 正确事件揭示
RequestHintButton.cs (line 165) Scene2 干扰项说明
RequestHintButton.cs (line 178) Scene3 hint 文案
问题：

这些 prompt 全写死在 RequestHintButton 里，策略和文案耦合严重。

Scene2GameManager.cs (line 459) TriggerAutoAI

Scene2GameManager.cs (line 481) 直接 RequestGuidance

问题：

Scene2 也自己拼 rules/userInput。
Scene3

Scene3GamePlayManager.cs (line 182) TriggerAutoAI
Scene3GamePlayManager.cs (line 188) 直接 RequestGuidance
Scene4

ItemHintController.cs (line 64) item 提示文案
AdvancedRecallController.cs (line 121) detail recall 错误时提示文案
问题：

Scene4 的两个 AI 入口也是散落的。

建议保留 AIInteractionManager 作为总入口，把“按场景拼 prompt”的工作也并进去。
CallAIManager 可以保留，但只做延迟触发，不再自己生成 userInput/componentRules。

建议新增的方法形态是“场景语义 API”，而不是继续传裸字符串。

通用方法

void RequestScenarioHint(AIScenario scenario, string extraRule = null)
逻辑：根据 scenario 从 MetaData 读当前上下文，统一组装 userInput、componentRules，再走 BuildFullPrompt。

void RequestAnswerFeedback(AIScenario scenario, bool isCorrect, string userAnswer, string correctAnswer, string targetName = null)
逻辑：统一处理答对/答错后的鼓励、安慰、隐式提示。

建议的场景枚举

Scene1ThemeHintSoft
Scene1ThemeReveal
Scene2NarrativeHintSoft
Scene2NarrativeReveal
Scene2DistractorExplain
Scene3SequencingHintSoft
Scene4ItemHintSoft
Scene4DetailQuestionHint
具体替换关系

RequestHintButton 里所有 TriggerRequest("长中文 prompt")
改成：

AIInteractionManager.Instance.RequestScenarioHint(AIScenario.Scene1ThemeHintSoft)
...Scene2NarrativeReveal
...Scene4ItemHintSoft
这类调用
ClickFunction、Scene2GameManager、Scene3GamePlayManager 里自己的 TriggerAutoAI
改成统一调用：

RequestAnswerFeedback(...)
CallAIManager 与 CallAIManagerButton
改成只负责：

延迟一帧
调一个统一方法，例如 AIInteractionManager.Instance.RequestDefaultFeedback()
不再各自维护 GetLatestUserInput/GetDynamicComponentRules