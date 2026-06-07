"""
İlm-i Sîmâ — Yüz Okuma Uygulaması (Backend)
--------------------------------------------
Bu küçük Flask sunucusu, tarayıcıdan gelen fotoğrafı alır,
Anthropic Claude API'sine gönderir ve ilm-i sîmâ yorumunu döndürür.

API anahtarın yalnızca burada (sunucu tarafında) durur,
tarayıcıya/internete asla sızmaz.

Çalıştırmak için README.md dosyasındaki adımları izle.
"""

import os
import json
import base64

from flask import Flask, request, jsonify, send_from_directory
import anthropic

# Doğum haritası hesabı (Moshier efemerisi — dış veri/internet gerektirmez)
try:
    import swisseph as swe
    ASTRO_OK = True
except Exception:
    ASTRO_OK = False

app = Flask(__name__, static_folder=".", static_url_path="")

# ---- Doğum haritası yardımcıları ----
ZODIAC_TR = ["Koç", "Boğa", "İkizler", "Yengeç", "Aslan", "Başak",
             "Terazi", "Akrep", "Yay", "Oğlak", "Kova", "Balık"]
ZODIAC_AR = ["الحَمَل", "الثَّوْر", "الجَوْزاء", "السَّرَطان", "الأَسَد", "العَذْراء",
             "المِيزان", "العَقْرَب", "القَوْس", "الجَدْي", "الدَّلْو", "الحُوت"]

PLANETS_TR = {}
if ASTRO_OK:
    PLANETS_TR = {
        "Güneş": swe.SUN, "Ay": swe.MOON, "Merkür": swe.MERCURY,
        "Venüs": swe.VENUS, "Mars": swe.MARS, "Jüpiter": swe.JUPITER,
        "Satürn": swe.SATURN,
    }


def _sign_of(lon):
    i = int(lon // 30) % 12
    return ZODIAC_TR[i], ZODIAC_AR[i], round(lon % 30, 1)


def compute_natal(year, month, day, hour=12.0):
    """Verilen tarih/saatten gezegen burçlarını döndürür."""
    if not ASTRO_OK:
        return None
    jd = swe.julday(year, month, day, hour)
    flag = swe.FLG_MOSEPH | swe.FLG_SPEED
    result = {}
    for name, pid in PLANETS_TR.items():
        res, _ = swe.calc_ut(jd, pid, flag)
        tr, ar, deg = _sign_of(res[0])
        result[name] = {"burc": tr, "burc_ar": ar, "derece": deg}
    return result

# API anahtarını ortam değişkeninden oku (güvenli yöntem)
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    print("\n[UYARI] ANTHROPIC_API_KEY ortam değişkeni ayarlanmamış!")
    print("Terminalde şunu çalıştır:  export ANTHROPIC_API_KEY='sk-ant-...'\n")

client = anthropic.Anthropic(api_key=API_KEY)

# Vision destekli, performans-maliyet dengeli model.
# İstersen "claude-opus-4-8" (en güçlü) veya "claude-haiku-4-5-20251001" (en ucuz) yapabilirsin.
MODEL = "claude-sonnet-4-6"

SIMA_PROMPT = """Sen İlm-i Sîmâ (fizyonomi) uzmanısın. İlm-i Sîmâ, İslam ve Osmanlı \
geleneğinde yüz hatlarından kişinin mizacını, ahlakını ve karakterini okuma ilmidir. \
İbn Arabî, Fahreddîn-i Râzî ve Osmanlı âlimlerinin geleneksel yüz okuma metodolojisini kullanıyorsun.

Gönderilen fotoğraftaki kişinin yüzünü GERÇEKTEN dikkatle incele: alın genişliği ve şekli, \
kaşların yapısı, gözlerin biçimi ve bakışı, burnun hattı, ağız ve dudaklar, çene ve genel yüz \
simetrisi. Bu somut gözlemlere dayanarak ilm-i sîmâ perspektifinden bir analiz yap.

ÖNEMLİ:
- Yorumlar gerçekten gördüğün yüz hatlarına dayansın, genel geçer olmasın.
- Bu eğlence ve kültürel bir uygulamadır; tıbbi/kesin iddialarda bulunma, klasik üslupta yorumla.
- Analizini sadece "sima_analizi" aracını çağırarak ver."""


# Tool use: çıktının her zaman geçerli yapıda gelmesini garantiler
SIMA_TOOL = {
    "name": "sima_analizi",
    "description": "İlm-i sîmâ yüz analizini yapılandırılmış biçimde döndürür.",
    "input_schema": {
        "type": "object",
        "properties": {
            "dominantTrait": {
                "type": "string",
                "description": "Baskın mizaç, Arapça ve harekeli (örn: الحِكْمَة)",
            },
            "dominantTraitTR": {"type": "string", "description": "Türkçe karşılığı"},
            "traits": {
                "type": "array",
                "description": "Beş yüz özelliği analizi",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "arabic": {"type": "string"},
                        "description": {"type": "string"},
                        "intensity": {"type": "integer"},
                    },
                    "required": ["name", "arabic", "description", "intensity"],
                },
            },
            "overall": {
                "type": "string",
                "description": "Bütünsel kıraat, 4-5 cümle, klasik Osmanlı üslubu",
            },
        },
        "required": ["dominantTrait", "dominantTraitTR", "traits", "overall"],
    },
}


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        data = request.get_json()
        image_b64 = data.get("image")
        media_type = data.get("mediaType", "image/jpeg")

        if not image_b64:
            return jsonify({"error": "Görsel bulunamadı"}), 400

        message = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            tools=[SIMA_TOOL],
            tool_choice={"type": "tool", "name": "sima_analizi"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": SIMA_PROMPT},
                    ],
                }
            ],
        )

        # Araç çıktısını al — bu her zaman geçerli bir sözlüktür
        result = None
        for block in message.content:
            if block.type == "tool_use" and block.name == "sima_analizi":
                result = block.input
                break

        if result is None:
            return jsonify({"error": "Model analiz üretmedi, tekrar dene."}), 502

        return jsonify(result)

    except anthropic.APIError as e:
        return jsonify({"error": f"API hatası: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"Beklenmeyen hata: {str(e)}"}), 500


# ---- KARMA: Yüz okuma + doğum haritası birleşik kıraat ----
KARMA_TOOL = {
    "name": "karma_kiraat",
    "description": "Yüz analizi ile doğum haritasını harmanlayan birleşik kıraati döndürür.",
    "input_schema": {
        "type": "object",
        "properties": {
            "baslik": {
                "type": "string",
                "description": "Bu kişiye özel arketipsel başlık (örn: 'Ateşin Vakarlı Bekçisi')",
            },
            "kopruler": {
                "type": "array",
                "description": "Yüz hattı ile gezegen yerleşimi arasında 3-4 köprü/uyum",
                "items": {
                    "type": "object",
                    "properties": {
                        "yuz": {"type": "string", "description": "Yüzdeki gözlem"},
                        "yildiz": {"type": "string", "description": "Haritadaki karşılığı"},
                        "yorum": {"type": "string", "description": "İkisini birleştiren tek cümle"},
                    },
                    "required": ["yuz", "yildiz", "yorum"],
                },
            },
            "kiraat": {
                "type": "string",
                "description": "Bütünsel karma kıraati, 5-6 cümle, klasik Osmanlı üslubu ama anlaşılır",
            },
        },
        "required": ["baslik", "kopruler", "kiraat"],
    },
}


@app.route("/karma", methods=["POST"])
def karma():
    try:
        data = request.get_json()
        image_b64 = data.get("image")
        media_type = data.get("mediaType", "image/jpeg")
        birth = data.get("birth", {})  # {year, month, day, hour}

        if not image_b64:
            return jsonify({"error": "Görsel bulunamadı"}), 400

        # Doğum haritasını hesapla
        try:
            year = int(birth.get("year"))
            month = int(birth.get("month"))
            day = int(birth.get("day"))
            hour = float(birth.get("hour", 12.0))
        except (TypeError, ValueError):
            return jsonify({"error": "Doğum tarihi eksik veya hatalı."}), 400

        natal = compute_natal(year, month, day, hour)
        if natal is None:
            return jsonify({"error": "Doğum haritası modülü kullanılamıyor."}), 500

        # Haritayı okunabilir metne çevir
        natal_text = "\n".join(
            f"- {p}: {v['burc']} burcu ({v['derece']}°)" for p, v in natal.items()
        )
        time_note = "Doğum saati verilmedi (öğlen varsayıldı), bu yüzden Ay ve iç gezegenler yaklaşıktır." \
            if birth.get("hour") in (None, "", 12.0) else ""

        karma_prompt = f"""Sen hem İlm-i Sîmâ (yüz okuma) hem de İlm-i Nücûm (doğum haritası) \
geleneğine hâkim bir Osmanlı müneccim-feraset üstadısın.

Bu kişinin YÜZÜNÜ fotoğraftan gerçekten incele. Aşağıda da doğum haritasındaki \
gezegen yerleşimleri var:

{natal_text}
{time_note}

Görevin: Yüzden okuduğun mizaç ile haritadaki gezegen yerleşimlerini TEK bir bütünsel \
kıraatte harmanlamak. Yüzdeki bir özelliğin haritadaki bir yerleşimle nasıl örtüştüğünü \
(veya gerilim oluşturduğunu) göster. Bu bir eğlence ve kültürel uygulamadır; klasik \
üslupla, akıcı ve kişiye özel yaz. Sadece 'karma_kiraat' aracını çağırarak cevap ver."""

        message = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            tools=[KARMA_TOOL],
            tool_choice={"type": "tool", "name": "karma_kiraat"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": karma_prompt},
                    ],
                }
            ],
        )

        result = None
        for block in message.content:
            if block.type == "tool_use" and block.name == "karma_kiraat":
                result = block.input
                break

        if result is None:
            return jsonify({"error": "Model kıraat üretmedi, tekrar dene."}), 502

        # Hesaplanan haritayı da geri gönder (arayüzde göstermek için)
        result["natal"] = natal
        return jsonify(result)

    except anthropic.APIError as e:
        return jsonify({"error": f"API hatası: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"Beklenmeyen hata: {str(e)}"}), 500


if __name__ == "__main__":
    # Bulutta (Render vb.) PORT ortam değişkeni gelir; lokalde 5000 kullanılır.
    port = int(os.environ.get("PORT", 5000))
    print("\n  İlm-i Sîmâ sunucusu başlatılıyor...")
    print(f"  Lokal kullanım:  http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)

