"""
notifier.py — Push Notification Pipeline (FCM)

Kullanım:
    from core.notifier import push_if_needed, send_push

Senaryo örnekleri:
    - "Rotanda yağmur başladı, dikkat!" (Weather Shield tetikler)
    - "Maç trafiği başlıyor" (Sports Intel tetikler)
    - "Favori istasyonunda yakıt ucuzladı" (Fuel Intel tetikler)
"""
import asyncio
from typing import Optional
from sqlmodel import select
from core.db import async_session_maker, UserPreference
from logger import log

# Firebase Admin SDK opsiyonel — olmasa bile sistem çalışmaya devam eder
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    _FIREBASE_AVAILABLE = True
except ImportError:
    _FIREBASE_AVAILABLE = False
    log.warning("⚠️ [FCM] firebase-admin paketi yüklü değil. Push notification devre dışı.")


def init_firebase(service_account_path: str = "firebase_credentials.json") -> bool:
    """
    Firebase Admin SDK'yı başlatır.
    service_account_path: Google Service Account JSON dosyası.
    Döner: True (başarılı) | False (hata veya paket yok)
    """
    if not _FIREBASE_AVAILABLE:
        return False

    if firebase_admin._apps:
        return True  # Zaten başlatılmış

    try:
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)
        log.success("✅ [FCM] Firebase Admin SDK başlatıldı.")
        return True
    except Exception as e:
        log.error(f"❌ [FCM] Firebase başlatma hatası: {e}")
        return False


async def send_push(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    icon: str = "ic_geo_notification",
    sound: str = "default",
) -> bool:
    """
    Tek bir cihaza FCM push notification gönderir.

    Args:
        fcm_token:  Cihazın FCM registration token'ı (Redis'te saklanır)
        title:      Bildirim başlığı
        body:       Bildirim metni
        data:       Ek payload (deep link, action_type vb.)
        icon:       Android bildirim ikonu (res/drawable)
        sound:      Ses ayarı ('default' veya özel ses dosyası adı)

    Returns:
        True → Gönderildi | False → Hata
    """
    if not _FIREBASE_AVAILABLE:
        log.debug(f"[FCM] (stub) '{title}': {body}")
        return False

    if not fcm_token:
        log.warning("[FCM] FCM token yok, bildirim atlandı.")
        return False

    try:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},   # FCM sadece string kabul eder
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    icon=icon,
                    sound=sound,
                    channel_id="geointel_alerts",
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound=sound, badge=1),
                )
            ),
            token=fcm_token,
        )

        # Firebase SDK blocking call → offload to thread
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, messaging.send, message)
        log.success(f"✅ [FCM] Bildirim gönderildi: {response}")
        return True

    except Exception as e:
        log.error(f"❌ [FCM] Bildirim gönderilemedi: {e}")
        return False


async def push_if_needed(
    redis_client,
    session_id: str,
    title: str,
    body: str,
    category: str = "general",
    data: Optional[dict] = None,
) -> bool:
    """
    Redis'ten FCM token'ı okur ve push gönderir.
    Redis yoksa veya token yoksa sessizce geçer.

    Args:
        redis_client:  Orchestrator'ın redis client'ı (None olabilir)
        session_id:    Kullanıcı session'ı
        title / body:  Bildirim içeriği
        category:      'weather' | 'traffic' | 'fuel' | 'sport' | 'general'
        data:          Ek payload
    """
    if not redis_client:
        return False

    try:
        fcm_token = redis_client.get(f"fcm:{session_id}")
        if not fcm_token:
            log.debug(f"[FCM] Session {session_id} için FCM token bulunamadı.")
            return False

        # Eğer session_id user_id içeriyorsa (user_id:session_id), tercihleri kontrol et
        if ":" in session_id:
            user_id = session_id.split(":")[0]
            try:
                from uuid import UUID
                uid = UUID(user_id)
                async with async_session_maker() as session:
                    pref_result = await session.execute(
                        select(UserPreference).where(
                            UserPreference.user_id == uid,
                            UserPreference.key == f"notify_{category}"
                        )
                    )
                    pref = pref_result.scalars().first()
                    # Tercih "false" olarak ayarlanmışsa bildirimi atla
                    if pref and pref.value.lower() in ("false", "0", "no"):
                        log.info(f"[FCM] Kullanıcı '{category}' bildirimlerini kapatmış. Atlanıyor.")
                        return False
            except Exception as e:
                log.warning(f"[FCM] Tercih kontrolü başarısız (görmezden geliniyor): {e}")

        payload = {"category": category, "session_id": session_id, **(data or {})}
        return await send_push(fcm_token=fcm_token, title=title, body=body, data=payload)
    except Exception as e:
        log.error(f"[FCM] push_if_needed hatası: {e}")
        return False


# ---------------------------------------------------------------------------
# HAZIR BİLDİRİM ŞABLONları
# ---------------------------------------------------------------------------

async def notify_weather_risk(redis_client, session_id: str, risk_zone: str, condition: str):
    """Rota hava riski bildirimi."""
    await push_if_needed(
        redis_client, session_id,
        title="⛈️ Rota Hava Uyarısı",
        body=f"{risk_zone} civarında {condition} bekleniyor. Dikkatli sür.",
        category="weather",
        data={"risk_zone": risk_zone, "condition": condition},
    )


async def notify_traffic_event(redis_client, session_id: str, event: str, impact: str):
    """Trafik etkinlik bildirimi (maç, konser vb.)."""
    await push_if_needed(
        redis_client, session_id,
        title="🚦 Trafik Uyarısı",
        body=f"{event} nedeniyle {impact} bölgesinde trafik yoğunluğu bekleniyor.",
        category="traffic",
        data={"event": event, "impact": impact},
    )


async def notify_fuel_deal(redis_client, session_id: str, station: str, price: float, fuel_type: str):
    """Yakıt fiyat fırsatı bildirimi."""
    await push_if_needed(
        redis_client, session_id,
        title="⛽ Yakıt Fırsatı",
        body=f"{station} istasyonunda {fuel_type}: {price:.2f} TL",
        category="fuel",
        data={"station": station, "price": str(price), "fuel_type": fuel_type},
    )
