from enum import Enum


class BotMode(str, Enum):
    OFF = "OFF"
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    SEMI_AUTO = "SEMI_AUTO"
    LIVE = "LIVE"
