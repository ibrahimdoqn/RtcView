# -*- coding: utf-8 -*-
"""
TapoMotionPersonDetector
========================

TP-Link Tapo C520WS (ve ONVIF uyumlu benzer Tapo kameralar) icin insan ve
hareket algilama durumunu ONVIF PullPoint Events uzerinden okuyan, arka
planda calisan, hataya dayanikli bir dedektor sinifi.

Bu modul, gercek kamera uzerinde yapilan testlerle dogrulanmis su ONVIF /
onvif-zeep davranislarini icerir (detaylar icin projenin README.md dosyasina
bakin):

  1. CreatePullPointSubscription cagrisi 'events' servisinden yapilmalidir,
     'pullpoint' servisinden degil (aksi halde "Service has no operation"
     hatasi alinir).
  2. PullMessages istek tipi, kutuphanenin kirilgan 'ns0' namespace takma
     adi yerine tam nitelikli namespace ile cekilmelidir (aksi halde
     "No element 'PullMessages' in namespace ...rw-2" hatasi alinir).
  3. Event Data alani zeep tarafindan parse edilemez (xs:any icerik modeli
     nedeniyle ham bir lxml Element'tir); lxml'in kendi API'siyle
     gezilmelidir.
  4. "RemoteDisconnected / Connection aborted" hatasi bu kamerada NORMAL bir
     davranistir (TP-Link firmware quirk'i), fatal degildir ve ayri, yuksek
     toleransli bir sayacla ele alinmalidir.

Kullanim:
    from tapo_detector import TapoMotionPersonDetector

    det = TapoMotionPersonDetector("192.168.31.8", 2020, "kullanici", "sifre")
    det.start()
    det.wait_until_connected(timeout=15)

    for state in det.stream(interval=1.0):
        print(state)   # {"motion": bool, "person": bool, "timestamp": str}

    det.stop()
"""

import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Generator, Optional, Tuple, List

import lxml.etree as ET
from onvif import ONVIFCamera

logger = logging.getLogger("tapo_detector")

# ONVIF Events servisinin WSDL namespace'i. PullMessages elementini
# kutuphanenin kirilgan 'ns0' takma adindan bagimsiz, tam nitelikli olarak
# cekmek icin kullanilir (bkz. modul docstring'i, madde 2).
_EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"

# Bu kamerada dogrulanmis ONVIF event topic'leri -> state alani eslemesi.
# Anahtar, gelen topic string'i icinde aranan alt metindir (tam eslesme
# aranmiyor cunku topic'ler 'tns1:RuleEngine/PeopleDetector/People' gibi
# on eklerle gelebiliyor).
_TOPIC_TO_FIELD = {
    "PeopleDetector/People": ("person", "IsPeople"),
    "CellMotionDetector/Motion": ("motion", "IsMotion"),
}


class TapoMotionPersonDetector:
    """
    ONVIF uzerinden bir Tapo kameradan insan/hareket algilama durumunu
    arka planda surekli okuyan sinif.

    Thread-safe: get_state() ve stream() herhangi bir thread'den güvenle
    çağrılabilir.
    """

    def __init__(
        self,
        ip: str,
        port: int,
        username: str,
        password: str,
        pull_timeout: float = 5.0,
        max_unexpected_errors: int = 15,
        max_benign_errors: int = 300,
    ):
        """
        Args:
            ip: Kameranin IP adresi.
            port: ONVIF portu (Tapo C520WS icin genellikle 2020).
            username: ONVIF kullanici adi.
            password: ONVIF sifresi.
            pull_timeout: Her PullMessages cagrisinin kamera tarafinda
                bekleyecegi maksimum sure (saniye).
            max_unexpected_errors: Bu kadar ardisik BEKLENMEYEN hatadan
                sonra abonelik yenilenir.
            max_benign_errors: Bu kadar ardisik "RemoteDisconnected" (bilinen/
                normal) hatasindan sonra abonelik yenilenir. Bu deger
                yuksek tutulmalidir cunku testlerde event'ler gelmeden once
                30-40 kadar bu hata gorulebiliyor.
        """
        self.ip = ip
        self.port = port
        self.username = username
        self.password = password
        self.pull_timeout = pull_timeout
        self.max_unexpected_errors = max_unexpected_errors
        self.max_benign_errors = max_benign_errors

        self._cam: Optional[ONVIFCamera] = None
        self._pullpoint = None
        self._pull_messages_type = None

        self._lock = threading.Lock()
        self._state: Dict = {
            "motion": False,
            "person": False,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Genel (public) API
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Arka plan dinleme thread'ini baslatir. Zaten calisiyorsa no-op."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._connected_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="tapo-detector", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Arka plan dinleme thread'ini durdurur ve bitmesini bekler."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def wait_until_connected(self, timeout: float = 15.0) -> bool:
        """
        Kameraya ilk basarili baglanti ve abonelik kuruluncaya kadar bekler.
        Basarili olursa True, timeout dolarsa False doner.
        """
        return self._connected_event.wait(timeout=timeout)

    def get_state(self) -> Dict:
        """
        Anlik durumu dondurur:
            {"motion": bool, "person": bool, "timestamp": ISO8601 string}
        """
        with self._lock:
            return dict(self._state)

    def stream(self, interval: float = 1.0) -> Generator[Dict, None, None]:
        """
        Her `interval` saniyede bir anlik durumu yield eder.

        Kullanim:
            for state in det.stream(interval=1.0):
                print(state)

        Durdurmak icin cagiran taraf donguden `break` edebilir ya da
        baska bir thread'den det.stop() cagirabilir (bu durumda stream()
        bir sonraki interval'de kendiliginden sona erer).
        """
        while not self._stop_event.is_set():
            yield self.get_state()
            time.sleep(interval)

    # ------------------------------------------------------------------
    # Ic mekanizma: baglanti / abonelik kurma
    # ------------------------------------------------------------------
    def _connect(self) -> bool:
        try:
            self._cam = ONVIFCamera(self.ip, self.port, self.username, self.password)
            info = self._cam.create_devicemgmt_service().GetDeviceInformation()
            logger.info(
                "Kameraya baglanildi: %s %s (fw %s)",
                info.Manufacturer,
                info.Model,
                info.FirmwareVersion,
            )
            return True
        except Exception as e:
            logger.warning("Kameraya baglanilamadi: %s", e)
            self._cam = None
            return False

    def _create_pullpoint(self) -> bool:
        try:
            # ONEMLI: CreatePullPointSubscription 'events' servisinden
            # cagrilmali (bkz. modul docstring'i, madde 1).
            events_service = self._cam.create_events_service()
            events_service.CreatePullPointSubscription()

            self._pullpoint = self._cam.create_pullpoint_service()

            # ONEMLI: 'ns0' bug'ini bypass etmek icin tam nitelikli
            # namespace ile element ceki­yoruz (bkz. madde 2).
            self._pull_messages_type = self._pullpoint.zeep_client.get_element(
                f"{{{_EVENTS_NS}}}PullMessages"
            )
            logger.info("PullPoint abonelik olusturuldu.")
            return True
        except Exception as e:
            logger.warning("Abonelik olusturulamadi: %s", e)
            self._pullpoint = None
            self._pull_messages_type = None
            return False

    # ------------------------------------------------------------------
    # Ic mekanizma: event parse (bkz. madde 3)
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_topic(msg) -> str:
        try:
            topic_obj = msg.Topic
            return (
                topic_obj._value_1
                if hasattr(topic_obj, "_value_1")
                else str(topic_obj)
            )
        except Exception:
            return ""

    @staticmethod
    def _extract_simple_items(msg) -> List[Tuple[str, str]]:
        """
        Event Data alanindaki SimpleItem (Name/Value) ciftlerini cikarir.
        msg.Message._value_1 genellikle zeep'in parse EDEMEDIGI ham bir
        lxml Element'tir (xs:any icerik modeli), bu yuzden lxml'in kendi
        API'siyle namespace'ten bagimsiz sekilde gezilir.
        """
        try:
            msg_content = msg.Message._value_1
            if not hasattr(msg_content, "iter"):
                return []
            items = []
            for el in msg_content.iter():
                tag = el.tag
                localname = (
                    ET.QName(tag).localname if isinstance(tag, str) else str(tag)
                )
                if localname == "SimpleItem":
                    items.append((el.get("Name"), el.get("Value")))
            return items
        except Exception:
            return []

    def _apply_event(self, raw_topic: str, items: List[Tuple[str, str]]) -> None:
        field = None
        value_key = None
        for key, (fname, vkey) in _TOPIC_TO_FIELD.items():
            if key in raw_topic:
                field, value_key = fname, vkey
                break
        if field is None:
            return  # izlenmeyen/bilinmeyen topic - sessizce atla

        for name, value in items:
            if name == value_key:
                bool_value = str(value).strip().lower() == "true"
                with self._lock:
                    self._state[field] = bool_value
                    self._state["timestamp"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                logger.debug("%s -> %s (topic=%s)", field, bool_value, raw_topic)
                return

    # ------------------------------------------------------------------
    # Ic mekanizma: ana dongu (bkz. madde 4 - hata toleransi)
    # ------------------------------------------------------------------
    def _sleep_interruptible(self, seconds: float) -> None:
        """stop_event set edilirse hemen uyanacak sekilde bekler."""
        end = time.time() + seconds
        while time.time() < end and not self._stop_event.is_set():
            time.sleep(0.2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._cam is None:
                if not self._connect():
                    self._sleep_interruptible(5.0)
                    continue

            if self._pullpoint is None:
                if not self._create_pullpoint():
                    self._cam = None
                    self._sleep_interruptible(5.0)
                    continue
                self._connected_event.set()

            self._listen_until_resubscribe_needed()

            # Buraya donulduyse abonelik/baglanti yenilenmeli
            self._pullpoint = None
            if not self._stop_event.is_set():
                self._sleep_interruptible(2.0)

    def _listen_until_resubscribe_needed(self) -> None:
        unexpected_errors = 0
        benign_errors = 0

        while not self._stop_event.is_set():
            try:
                req = self._pull_messages_type(
                    Timeout=timedelta(seconds=self.pull_timeout),
                    MessageLimit=50,
                )
                response = self._pullpoint.PullMessages(req)
                unexpected_errors = 0
                benign_errors = 0

                if response and response.NotificationMessage:
                    for msg in response.NotificationMessage:
                        raw_topic = self._extract_topic(msg)
                        items = self._extract_simple_items(msg)
                        self._apply_event(raw_topic, items)

            except Exception as e:
                err_str = str(e)

                # Bilinen/beklenen davranis: sessizce tekrar dene, ayri
                # (yuksek toleransli) sayacla takip et.
                if "RemoteDisconnected" in err_str or "Connection aborted" in err_str:
                    benign_errors += 1
                    if benign_errors >= self.max_benign_errors:
                        logger.warning(
                            "%d ardisik baglanti hatasi, abonelik yenileniyor.",
                            benign_errors,
                        )
                        return
                    time.sleep(0.3)
                    continue

                # Gercekten beklenmeyen hata
                unexpected_errors += 1
                logger.warning("Beklenmeyen pull hatasi: %s", e)
                if unexpected_errors >= self.max_unexpected_errors:
                    logger.warning(
                        "%d ardisik beklenmeyen hata, abonelik yenileniyor.",
                        unexpected_errors,
                    )
                    return
                time.sleep(0.3)
