"""
guard.py — İlm-i Sîmâ için kota + maliyet kalkanı
=================================================
Amaç: uygulama halka açıldığında Anthropic faturasının kontrolden çıkmasını
ve tek bir kişinin script'le yüzlerce istek atmasını engellemek.

Tasarım kararları:
- Bellek içi (dict). Render'da `--workers 1` çalıştığı için bu YETERLİ.
  Worker sayısını artırırsan burayı Redis'e taşımalısın (Upstash ücretsiz katman).
- Anahtar = cihaz kimliği + IP. Türkiye'de mobil operatörler CGNAT kullanır,
  yani binlerce kişi aynı IP'den çıkabilir. Sadece IP ile sınırlarsan
  gerçek kullanıcıları kaparsın. Bu yüzden:
    * cihaz başına  -> sıkı kota (asıl fren)
    * IP başına     -> gevşek tavan (script saldırısına karşı)
- Günlük global USD tavanı: aşılırsa uygulama okumayı reddeder, üstad susar.
  Fatura sürprizi yerine kontrollü "bugünlük bu kadar" mesajı.

Kurulum: app.py'nin en üstüne
    from guard import korumali, kaydet_kullanim, guard_durum
Sonra rotaların üzerine dekoratörü ekle (aşağıda ENTEGRASYON bölümü).
"""

import os
import time
import threading
from functools import wraps
from datetime import datetime, timezone

from flask import request, jsonify

# ---------------------------------------------------------------- ayarlar
# Hepsi ortam değişkeniyle ezilebilir — kod değiştirmeden Render panelinden ayarla.

def _f(ad, varsayilan):
    try:
        return float(os.environ.get(ad, varsayilan))
    except ValueError:
        return float(varsayilan)

def _i(ad, varsayilan):
    try:
        return int(os.environ.get(ad, varsayilan))
    except ValueError:
        return int(varsayilan)

# Günlük toplam harcama tavanı (USD). Bunu aşınca tüm pahalı uçlar kapanır.
GUNLUK_USD_TAVAN = _f("GUNLUK_USD_TAVAN", "5.0")

# Uç bazlı kotalar: (cihaz başına / gün, IP başına / gün)
KOTALAR = {
    "analyze": (_i("KOTA_ANALYZE", 2), _i("KOTA_IP_ANALYZE", 40)),
    "karma":   (_i("KOTA_KARMA", 1),   _i("KOTA_IP_KARMA", 25)),
    "eslesme": (_i("KOTA_ESLESME", 2), _i("KOTA_IP_ESLESME", 30)),
    "gunluk":  (_i("KOTA_GUNLUK", 3),  _i("KOTA_IP_GUNLUK", 120)),
    "ses":     (_i("KOTA_SES", 10),    _i("KOTA_IP_SES", 200)),
    # Sohbet ucuz (Haiku, 400 token) — cömert olabiliriz.
    "sor":     (_i("KOTA_SOR", 25),    _i("KOTA_IP_SOR", 400)),
}

# Ani seri (burst) freni: aynı cihazdan iki ağır istek arası minimum saniye.
MIN_ARALIK_SN = _i("MIN_ARALIK_SN", 8)

# Model fiyatları — USD / 1M token (input, output).
# Kaynak: platform.claude.com/docs/en/about-claude/pricing — değişebilir, ara ara doğrula.
FIYAT = {
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-sonnet-5":           (2.00, 10.00),
    "claude-haiku-4-5-20251001": (1.00,  5.00),
    "claude-haiku-4-5":          (1.00,  5.00),
    "claude-opus-5":             (5.00, 25.00),
}
VARSAYILAN_FIYAT = (3.00, 15.00)

# ---------------------------------------------------------------- durum
_kilit = threading.Lock()
_gun = None                 # "2026-08-20"
_sayac = {}                 # {(uc, "c:abc" | "i:1.2.3.4"): adet}
_son_istek = {}             # {cihaz_id: timestamp}
_harcama = {"usd": 0.0, "istek": 0, "in": 0, "out": 0}
_uc_harcama = {}            # {uc: usd}


def _bugun():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _gunu_tazele():
    """Gün değiştiyse tüm sayaçları sıfırla."""
    global _gun
    b = _bugun()
    if _gun != b:
        _gun = b
        _sayac.clear()
        _son_istek.clear()
        _uc_harcama.clear()
        _harcama.update({"usd": 0.0, "istek": 0, "in": 0, "out": 0})


def _cihaz_id():
    """Frontend'in gönderdiği kalıcı cihaz kimliği.
    index.html şunu yapmalı:
        let cid = localStorage.getItem('sima_cid');
        if (!cid) { cid = crypto.randomUUID(); localStorage.setItem('sima_cid', cid); }
        fetch(..., { headers: { 'X-Sima-Cihaz': cid, 'Content-Type': 'application/json' } })
    Silinebilir bir kimlik — tek başına güvenlik değil, IP tavanıyla birlikte çalışır.
    """
    cid = (request.headers.get("X-Sima-Cihaz") or "").strip()[:64]
    return cid or "anonim"


def _ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


# ---------------------------------------------------------------- API

def kaydet_kullanim(model, in_tok, out_tok, uc="?"):
    """Gerçek token kullanımını maliyete çevirip kaydeder.
    gemini_json() içinden çağrılmalı (aşağıdaki entegrasyon notuna bak)."""
    g, c = FIYAT.get(model, VARSAYILAN_FIYAT)
    usd = (in_tok / 1_000_000) * g + (out_tok / 1_000_000) * c
    with _kilit:
        _gunu_tazele()
        _harcama["usd"] += usd
        _harcama["istek"] += 1
        _harcama["in"] += in_tok
        _harcama["out"] += out_tok
        _uc_harcama[uc] = _uc_harcama.get(uc, 0.0) + usd
    return usd


def korumali(uc, agir=True):
    """Rota dekoratörü.
    uc:   KOTALAR sözlüğündeki anahtar
    agir: True ise günlük USD tavanına ve burst frenine tabi."""
    def sarmalayici(fn):
        @wraps(fn)
        def ic(*a, **kw):
            cihaz = _cihaz_id()
            ip = _ip()
            simdi = time.time()

            with _kilit:
                _gunu_tazele()

                # 1) Global bütçe tavanı
                if agir and _harcama["usd"] >= GUNLUK_USD_TAVAN:
                    return jsonify({
                        "error": "kota_gunluk",
                        "mesaj": "Üstad bugünlük kıraatini tamamladı. "
                                 "Yarın sabah divan yeniden kurulur."
                    }), 503

                # 2) Burst freni
                if agir:
                    onceki = _son_istek.get(cihaz, 0)
                    if simdi - onceki < MIN_ARALIK_SN:
                        kalan = int(MIN_ARALIK_SN - (simdi - onceki)) + 1
                        return jsonify({
                            "error": "cok_hizli",
                            "mesaj": f"Üstad henüz nefeslenmedi. {kalan} saniye bekle."
                        }), 429
                    _son_istek[cihaz] = simdi

                # 3) Cihaz ve IP kotası
                cihaz_kota, ip_kota = KOTALAR.get(uc, (5, 50))
                ck = (uc, "c:" + cihaz)
                ik = (uc, "i:" + ip)
                if _sayac.get(ck, 0) >= cihaz_kota:
                    return jsonify({
                        "error": "kota_cihaz",
                        "mesaj": "Bugünlük sîmâ hakkın doldu. Bir sîmâ günde bir kez değişir; "
                                 "yarın yeniden bak."
                    }), 429
                if _sayac.get(ik, 0) >= ip_kota:
                    return jsonify({
                        "error": "kota_ip",
                        "mesaj": "Bu ağdan çok fazla istek geldi. Biraz sonra tekrar dene."
                    }), 429

                _sayac[ck] = _sayac.get(ck, 0) + 1
                _sayac[ik] = _sayac.get(ik, 0) + 1

            return fn(*a, **kw)
        return ic
    return sarmalayici


def guard_durum():
    """Basit gözlem ucu. app.py'ye ekle:
        @app.route("/_durum")
        def durum():
            if request.args.get("k") != os.environ.get("DURUM_ANAHTARI"): return "", 404
            return jsonify(guard_durum())
    """
    with _kilit:
        _gunu_tazele()
        return {
            "gun": _gun,
            "harcama_usd": round(_harcama["usd"], 4),
            "tavan_usd": GUNLUK_USD_TAVAN,
            "doluluk_yuzde": round(100 * _harcama["usd"] / GUNLUK_USD_TAVAN, 1),
            "istek": _harcama["istek"],
            "input_token": _harcama["in"],
            "output_token": _harcama["out"],
            "uc_bazli_usd": {k: round(v, 4) for k, v in _uc_harcama.items()},
            "tekil_cihaz": len({k[1] for k in _sayac if k[1].startswith("c:")}),
        }


# ---------------------------------------------------------------- ENTEGRASYON
#
# 1) app.py başına:
#        from guard import korumali, kaydet_kullanim, guard_durum
#
# 2) gemini_json() içinde, message döndükten HEMEN SONRA:
#
#        message = client.messages.create(...)
#        try:
#            kaydet_kullanim(MODEL, message.usage.input_tokens,
#                            message.usage.output_tokens, uc=uc_adi)
#        except Exception:
#            pass
#
#    (gemini_json'a `uc_adi="?"` diye bir parametre ekle, çağrılarda geç.)
#
# 3) Rotaların üzerine:
#        @app.route("/analyze", methods=["POST"])
#        @korumali("analyze")
#        def analyze(): ...
#
#        @app.route("/karma", methods=["POST"])
#        @korumali("karma")
#        def karma(): ...
#
#        @app.route("/gunluk", methods=["POST"])
#        @korumali("gunluk", agir=False)
#        def gunluk(): ...
#
#        @app.route("/eslesme", methods=["POST"])
#        @korumali("eslesme")
#        def eslesme(): ...
#
#        @app.route("/ses", methods=["POST"])
#        @korumali("ses", agir=False)
#        def ses(): ...
#
# 4) /gelecek ve /el uçlarını SİL. Kullanılmıyorlar ama açık bir kapıdan
#    para harcatılabilir. Ölü kod = açık saldırı yüzeyi.
#
# 5) Frontend'de 429/503 dönüşünde error kutusuna `mesaj` alanını bas,
#    ham HTTP hatası gösterme.
