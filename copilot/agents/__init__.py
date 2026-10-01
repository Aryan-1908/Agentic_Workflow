"""The copilot's agents (spec M6). Step 1: the Investigator and the Recommender, passing a structured Investigation.
Every agent follows the cost rule: code first, an LLM only when the cheap path can't decide."""
from .investigator import Investigation, Investigator
from .recommender import Recommender
