import json
from typing import Any, Dict

from jinja2 import Template
from twelvelabs import ResponseFormat

from my_basics import ActivityContext, EntityContext, parse_json_from_llm


class VideoProcessor:
    def __init__(
        self,
        client,
        index_id: str,
        video_path: str,
    ):
        self.client = client
        self.index_id = index_id
        self.video_path = video_path
        self.video_id = self.create_video_task(video_path)
        
    def create_video_task(self, video_path: str):
        with open(video_path, "rb") as f:
            task = self.client.tasks.create(
                index_id=self.index_id, 
                video_file=(f.name, f, "video/mp4"),
            )
            print(f"Created task: id={task.id}, video_id={task.video_id}")
            video_id = task.video_id
        return video_id
    

    def analyze_events(self, ctx: ActivityContext) -> Dict[str, Any]:
        with open("prompts/event_split_prompt.md", "r", encoding="utf-8") as f:
            template = Template(f.read())
            prompt = template.render(
                activity=ctx.activity,
                people=ctx.people,
                time=ctx.time,
                location=ctx.location,
                )
        # print("------Event split prompt------\n")
        # print(prompt)

        if self.video_id:
            event_resp = self.client.analyze(
                video_id=self.video_id,
                prompt=prompt,
                temperature=0.2,
                response_format=ResponseFormat(
                    json_schema={
                        "activity_description": "string",
                        "event_count": "integer",
                        "events": [
                            {
                                "id": "string",
                                "description": "string",
                                "start_time": "HH:MM:SS",
                                "end_time": "HH:MM:SS",
                                "scene_clues": [
                                    {"frame": "HH:MM:SS", "description": "string"}
                                ],
                            }
                        ],
                    },
                ),
            )

            if not event_resp.data:
                return {}
            event_parsed = parse_json_from_llm(event_resp.data)
            print("----- Event split response (parsed JSON) ------\n")
            print(json.dumps(event_parsed, ensure_ascii=False, indent=2))
            return event_parsed

        else:
            raise ValueError("Invalid VIDEO_ID.")
    

    def analyze_entities(
        self,
        ctx: EntityContext,
        event_id: str,
        event_description: str,
        start_ts: str,
        end_ts: str,
    ) -> Dict[str, Any]:
        with open("prompts/entity_extract_prompt.md", "r", encoding="utf-8") as f:
            template = Template(f.read())
            entity_prompt = template.render(
                activity=ctx.activity,
                people=ctx.people,
                time=ctx.time,
                sub_event=ctx.sub_event.strip("。") if ctx.sub_event else ctx.sub_event,
                event_time_range=f"{ctx.start_time} - {ctx.end_time}",
            )
        # print("------Entity Extraction Prompt------\n")
        # print(entity_prompt)

        if self.video_id:
            entity_resp = self.client.analyze(
                video_id=self.video_id,
                prompt=entity_prompt,
                response_format=ResponseFormat(
                    json_schema={
                        "key_persons": [
                            {
                                "id": "string",
                                "item_name": "string",
                                "key_frame": "HH:MM:SS",
                                "coordinates": {
                                    "x": "integer",
                                    "y": "integer",
                                    "width": "integer",
                                    "height": "integer",
                                },
                                "details": {
                                    "visual": ["string"],
                                    "semantic": ["string"],
                                },
                                "interaction": "string",
                            }
                        ],
                        "key_objects": [
                            {
                                "id": "string",
                                "item_name": "string",
                                "key_frame": "HH:MM:SS",
                                "coordinates": {
                                    "x": "integer",
                                    "y": "integer",
                                    "width": "integer",
                                    "height": "integer",
                                },
                                "details": {
                                    "visual": ["string"],
                                    "semantic": ["string"],
                                },
                                "interaction": "string",
                            }
                        ],
                    }
                ),
            )

            print("------ Entity extract response ------\n")
            print(f"Event {event_description}, start: {start_ts}, end: {end_ts}\n")
            if not entity_resp.data:
                print("No data returned from LLM.")
                return {}
            entity_parsed = parse_json_from_llm(entity_resp.data)
            print(json.dumps(entity_parsed, ensure_ascii=False, indent=2))
            return entity_parsed
        
        else:
            raise ValueError("Invalid VIDEO_ID.")
