# -*- coding: utf-8 -*-
"""
tapo_detector
=============

TP-Link Tapo ONVIF kameralarindan insan/hareket algilama durumunu okumak
icin kucuk bir yardimci paket.
"""

from .detector import TapoMotionPersonDetector

__all__ = ["TapoMotionPersonDetector"]
__version__ = "1.0.0"
