from enum import Enum
import json
import datetime
from jinja2 import Template
from flask import Flask, jsonify
from openai import OpenAI

class task_level(Enum):
    FRAMEWORK = "framework"
    NARRATIVE = "narrative"
    ENTITY = "entity"

class task_stage(Enum):
    START = "start"
    INPROG = "in_progress"
    END = "ending"
    
client = OpenAI(
    base_url='https://api.openai-proxy.org/v1',
    api_key='sk-myAPIkeysxxxxxxxxxxxxxxxxx'
)

app = Flask(__name__)

class RecallAssistant:
    def __init__(self, memory_perf_path, task_context_path, activity_context_path):
        """初始化并加载上下文"""
        with open(memory_perf_path, 'r') as f:
            self.memory_perf = json.load(f)
        with open(task_context_path, 'r') as f:
            self.task_context = json.load(f)
        with open(activity_context_path, 'r') as f:
            self.activity_context = json.load(f)
        
        self.model_name = "gpt-4o"
        self.api_key = "openai_api_key"
        self.response_timeout = 10
    
    def get_stage(self):
        """从task_context里获取当前任务阶段信息
        """
        return self.task_context['task_stage']['stage']
    
    def get_memory_state(self):
        """分析用户记忆状态"""
        last_response_str = self.memory_perf['response_metrics']['last_response_timestamp']
        current_time = datetime.datetime.now(datetime.timezone.utc)
        # mockup
        # current_time = datetime.datetime.fromisoformat("2024-06-15T17:05:20+8:00")
        try:
            last_response_time = datetime.datetime.fromisoformat(last_response_str)
            time_diff = (current_time - last_response_time).total_seconds()
        except ValueError:
            time_diff = 0
        
        # 判断逻辑：当前错误>0 或 请求提示 或 超过10s未响应则判定为负向状态
        if (self.memory_perf['error_metrics']['current_errors'] > 0 or
            self.memory_perf['hint_metrics']['hint_requested'] or
            time_diff > self.response_timeout):
            return "negative"
        return "positive"
    
    def select_strategy(self, stage, memory_state):
        """根据任务阶段和记忆状态选择对话策略组件"""
        strategy_matrix = {
            "start": {
                "positive": ["探询"]
            },
            "in_progress": {
                "positive": ["祝贺", "重复"],
                "negative": ["提示", "安慰", "校正"]
            },
            "ending": {
                "positive": ["总结", "祝贺"],
                "negative": ["安慰", "总结"]
            }
        }
        return strategy_matrix.get(stage, {}).get(memory_state, ["探询"]) 
    
    def get_clues(self):
        """根据当前任务上下文和记忆表现获取渐进式线索"""
        current_level = self.task_context['current_level']['name']
        hint_count = self.memory_perf['hint_metrics']['hint_count']
        error_count = self.memory_perf['error_metrics']['current_errors']
        
        clue_level = min(max(hint_count, error_count), 2)
 
        if current_level == "framework":
            return self.get_theme_clues(clue_level)
        elif current_level == "narrative":
            return self.get_scene_clues(clue_level)
        elif current_level == "entity":
            return self.get_entity_clues(clue_level)
        else:
            return {"text": [], "visual": [], "audio": []}
        
    def get_theme_clues(self, clue_level):
        """框架层线索：活动主题提示"""
        clues = {"text": [], "visual": [], "audio": []}
        activity = self.activity_context['activity']
        
        if clue_level == 0:
            clues['text'] = [
                f"{activity['time_range']}",
                f"{activity['location']}"
            ]
        elif clue_level == 1:
            clues['audio'] = self.activity_context.get('audio_clues', [])
            if not clues['audio']:
                clue_level = 2
                clues['visual'] = self.activity_context.get('visual_cues', [])
        else:
            clues['visual'] = self.activity_context.get('visual_cues', [])
        
        return clues
     
    def get_scene_clues(self, clue_level): 
        """叙事层线索：场景/行为提示""" 
        clues = {"text": [], "visual": [], "audio": []}
        current_scene = self.task_context['retrieval_context']['current_target']['scene']
        
        if clue_level == 0:
            clues['visual'] = self.activity_context[current_scene]['visual_cues']
        elif clue_level == 1:
            clues['audio'] = self.activity_context[current_scene].get('audio_clues', [])
            if not clues['audio']:
                clue_level = 2
        else: 
            clues['text'] = self.activity_context[current_scene].get('text_clues', [])
        
        return clues
    
    def get_entity_clues(self, clue_level):
        """实体层线索：人物/物体提示"""
        clues = {"text": [], "visual": [], "audio": []}
        scene_name = self.task_context['scene']
        target = self.task_context['retrieval_context']['current_target']
        target_name = target['item_name']
        
        if clue_level == 0:
            clues['text'] = self.activity_context[scene_name][target_name].get('text_clues', []) 
        elif clue_level == 1: 
            clues['audio'] = self.activity_context[scene_name][target_name].get('audio_clues', [])
            if not clues['audio']: 
                clues['visual'] = self.activity_context[scene_name][target_name].get('visual_clues', []) 
                if not clues['visual']: 
                    clues['text'] = self.activity_context[scene_name][target_name].get('text_clues', []) 
        else: 
            entity_image = self.activity_context[scene_name][target_name].get('entity_image', []) 
            if entity_image: 
                bounding_box = target['bounding_box']
                 
                clues['visual'] = [ 
                    { 
                        "image": entity_image, 
                        "type": "progressive", 
                        "steps": [ 
                            {"time": 0, "action": "mask", "area": bounding_box}, 
                            {"time": 5, "action": "reveal"}, 
                            {"time": 10, "action": "highlight", "area": bounding_box}
                        ]
                    }
                ]
            else: 
                clues['text'] = [f"{target['target_description']}"]
         
        return clues
 
    def generate_guide_prompt(self, strategy_components): 
        """构建记忆引导提示词""" 
        # 提取上下文信息 
        context = self.task_context['retrieval_context']
        perf = self.memory_perf 
         
        # 准备提示词变量 
        prompt_vars = {
            "task_level": self.task_context['current_level']['name'],
            "task_type": self.task_context['current_task']['task_type'],
            "activity_theme": context['parent_activity']['theme'],
            "events": [e['scene'] for e in context['previous_retrievals']],
            "items": context['current_target']['expected_actions'],
            "retrieved_info": [item['item'] for item in perf['current_level_progress']['retrieved_items']],
            "component_rules": "，".join(strategy_components),
            "output_example": """{ "dialogues": [ { "id": 1, "strategy": "祝贺", "content": "很好！你记起来了，确实是在窗边坐下的。" }, { "id": 2, "strategy": "重复", "content": "你们选择了靠窗的位置坐下，那里的视野很不错。" } ] }"""
        }
         
        with open("prompts/entity_extract_prompt.md", "r", encoding="utf-8") as f:
            template = Template(f.read())
            prompt = template.render(**prompt_vars)

        return prompt
     
    def get_response(self):
        """生成记忆引导内容"""
        # 获取任务阶段和记忆状态
        stage = self.get_stage()
        memory_state = self.get_memory_state() if stage == "in_progress" else None
         
        # 选择策略并获取线索
        strategy_components = self.select_strategy(stage, memory_state)
        clues = self.get_clues() if memory_state == "negative" else None
        
        # 生成引导提示词
        prompt = self.generate_guide_prompt(strategy_components)
        
        # 调用GPT-4o API生成引导内容
        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", "content": prompt}], 
                temperature=0.3,
                max_tokens=500
            )
            if response.choices[0].message.content:
                guide_content = json.loads(response.choices[0].message.content)
            else:
                guide_content = {"dialogues": [{
                "id": 1,
                "strategy": "",
                "content": ""
            }]}
        except Exception as e:
            # API调用失败时返回默认引导
            guide_content = {"dialogues": [{
                "id": 1,
                "strategy": "",
                "content": ""
            }]}
         
        # 构建最终响应
        return {
            "stage": stage,
            "memory_state": memory_state,
            "strategies": strategy_components,
            "clues": clues,
            "guide_content": guide_content
        }

@app.route('/get_guide', methods=['POST'])
def get_guide():
    assistant = RecallAssistant('memory_performance.json',
                                'task_context.json',
                                "activity_context.json")
    response = assistant.get_response() 
    return jsonify(response)

if __name__ == '__main__': 
    app.run(host='0.0.0.0', port=5000, debug=True)