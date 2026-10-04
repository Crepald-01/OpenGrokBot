"""Example OpenGrokBot Python plugin.

register(api) returns a list of tool dicts:
    name         tool name (the plugin id is prefixed automatically: example-python_text_stats)
    description  what the model reads
    input_schema JSON Schema for the arguments
    handler      callable(ctx, args) or callable(args) -> str | dict | list  (the result is treated as untrusted data)
    risk         "safe" (default) or one of: send, write, delete, submit, purchase, command  -> the user is asked first

api.config(key, default) reads the plugin's settings (non-secret and secret fields from plugin.json).
api.http(ctx, "GET", url, ...) makes HTTP requests that respect the Bot's network policy.
"""
import random


def register(api):
    def text_stats(args):
        text = args.get("text", "")
        words = text.split()
        return {"characters": len(text), "words": len(words), "greeting": api.config("greeting", "Hello")}

    def roll(args):
        sides = max(2, min(int(args.get("sides", 6)), 1000))
        return {"sides": sides, "result": random.randint(1, sides)}

    def save_note(args):   # a "write" tool: the user is asked to approve each call
        return f"(pretend) saved note: {args.get('text', '')[:100]}"

    return [
        {"name": "text_stats", "description": "Count characters and words in a piece of text.",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, "handler": text_stats},
        {"name": "roll_dice", "description": "Roll a die with the given number of sides.",
         "input_schema": {"type": "object", "properties": {"sides": {"type": "integer"}}}, "handler": roll},
        {"name": "save_note", "description": "Example of a consequential tool that needs approval.",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, "handler": save_note, "risk": "write"},
    ]
