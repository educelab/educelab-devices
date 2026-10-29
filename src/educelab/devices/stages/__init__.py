"""Stages: devices that position things along linear or rotary axes."""

from educelab.devices.stages.base import Axis, AxisKind, Stage
from educelab.devices.stages.foldio360 import Foldio360
from educelab.devices.stages.simulated import SimulatedStage
from educelab.devices.stages.xcarve import XCarve

__all__ = ["Axis", "AxisKind", "Foldio360", "SimulatedStage", "Stage", "XCarve"]
