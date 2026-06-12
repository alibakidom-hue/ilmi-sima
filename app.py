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


# ---- Ebced (hisâb el-cümel) hesabı ----
# Standart ebced-i kebir değerleri (Arapça harf -> sayı)
EBCED_VALUES = {
    "ا": 1, "ب": 2, "ج": 3, "د": 4, "ه": 5, "و": 6, "ز": 7, "ح": 8, "ط": 9,
    "ي": 10, "ك": 20, "ل": 30, "م": 40, "ن": 50, "س": 60, "ع": 70, "ف": 80,
    "ص": 90, "ق": 100, "ر": 200, "ش": 300, "ت": 400, "ث": 500, "خ": 600,
    "ذ": 700, "ض": 800, "ظ": 900, "غ": 1000,
}

# Türkçe Latin harf -> en yakın Arapça harf (deterministik yaklaşım)
TR_TO_AR = {
    "a": "ا", "â": "ا", "b": "ب", "c": "ج", "ç": "ج", "d": "د", "e": "ه",
    "f": "ف", "g": "ك", "ğ": "غ", "h": "ه", "ı": "ا", "i": "ي", "î": "ي",
    "j": "ز", "k": "ك", "l": "ل", "m": "م", "n": "ن", "o": "و", "ö": "و",
    "p": "ب", "r": "ر", "s": "س", "ş": "ش", "t": "ت", "u": "و", "ü": "و",
    "û": "و", "v": "و", "y": "ي", "z": "ز",
}


def compute_ebced(name):
    """Türkçe ismin yaklaşık ebced (kebir) değerini ve Arapça harf dizisini döndürür."""
    if not name:
        return None
    arabic_letters = []
    total = 0
    breakdown = []
    for ch in name.lower():
        ar = TR_TO_AR.get(ch)
        if ar:
            val = EBCED_VALUES.get(ar, 0)
            arabic_letters.append(ar)
            total += val
            breakdown.append({"harf": ch, "arapca": ar, "deger": val})
    if not arabic_letters:
        return None
    return {
        "isim_arapca": "".join(arabic_letters),
        "toplam": total,
        "dokum": breakdown,
    }

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

Gönderilen fotoğraftaki kişinin yüzünü GERÇEKTEN dikkatle, ayrıntılı incele.

ŞU ALTI OKUMA YERİNİN HER BİRİ İÇİN AYRI bir özellik (trait) üret (toplam 6 trait):
1. ALIN (الجَبْهَة): genişliği, yüksekliği, çıkıntısı, varsa çizgileri — firâsetin en önemli yeri, ayrıntılı oku.
2. KAŞLAR (الحَواجِب): kalınlığı, çatıklığı, kavisi, birbirine yakınlığı.
3. GÖZLER (العُيُون): biçimi, büyüklüğü, bakışın derinliği/canlılığı.
4. BURUN (الأَنْف): hattı, kemeri, ucu, genişliği.
5. AĞIZ VE DUDAKLAR (الشِّفَاه): dudak dolgunluğu, ağız hattı, kapanışı.
6. ÇENE VE YÜZ HATTI (الذَّقَن): çenenin gücü/biçimi, yüzün genel simetrisi ve oranları.

Her trait'in 'name' alanı yukarıdaki Türkçe adı, 'arabic' alanı yanındaki Arapça karşılığı olsun.

ÖNEMLİ:
- Yorumlar gerçekten gördüğün yüz hatlarına dayansın, genel geçer olmasın.
- DENGELİ ol: her özellikte hem güçlü yönü hem de zaafı/gölge tarafı belirt. İlm-i sîmâ \
salt övgü değildir; bir hattın hem meziyetini hem de dizginlenmezse nereye kayabileceğini söyler. \
Mesela "kararlılık gösterir, ama bu inat ve esneksizliğe dönüşebilir" gibi. En az iki özellikte \
gerçek bir zaaf/gerilim/uyarı bulunsun. Yağcılık yapma, dürüst ama yapıcı ol.
- Bu eğlence ve kültürel bir uygulamadır; tıbbi/kesin iddialarda bulunma, klasik üslupta yorumla. \
Kişiyi yıkmadan, ama gerçekçi şekilde gölge yönleri de göster.
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
            max_tokens=2600,
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
            "guclu_yanlar": {
                "type": "array",
                "description": "Bu kişinin 3-4 güçlü/parlak yanı, kısa maddeler",
                "items": {"type": "string"},
            },
            "golge_yanlar": {
                "type": "array",
                "description": "Bu kişinin 3-4 zaafı/gölge yanı, dürüst ama kırıcı olmayan kısa maddeler",
                "items": {"type": "string"},
            },
            "dikkat_edilecekler": {
                "type": "array",
                "description": "Bu mizaçla daha iyi anlaşmak/iletişim kurmak için 2-3 pratik not (örn: 'kararlarını acele bekleme'). 'Bu kişiden sakın' gibi yargı DEĞİL, yapıcı tavsiye.",
                "items": {"type": "string"},
            },
            "ebced_yorum": {
                "type": "string",
                "description": "İsim verildiyse, ebced sayısının kısa yorumu (2-3 cümle). İsim yoksa boş bırak.",
            },
        },
        "required": ["baslik", "kopruler", "kiraat", "guclu_yanlar", "golge_yanlar", "dikkat_edilecekler"],
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

        # Ebced (isim verildiyse)
        name = (data.get("name") or "").strip()
        ebced = compute_ebced(name) if name else None

        # Haritayı okunabilir metne çevir
        natal_text = "\n".join(
            f"- {p}: {v['burc']} burcu ({v['derece']}°)" for p, v in natal.items()
        )
        time_note = "Doğum saati verilmedi (öğlen varsayıldı), bu yüzden Ay ve iç gezegenler yaklaşıktır." \
            if birth.get("hour") in (None, "", 12.0) else ""

        ebced_text = ""
        if ebced:
            ebced_text = (
                f"\nAYRICA kişinin ismi '{name}' — ebced (hisâb el-cümel) değeri {ebced['toplam']} "
                f"(Arapça harflerle: {ebced['isim_arapca']}). Bu sayıyı da kıraate kat ve "
                f"'ebced_yorum' alanında kısaca yorumla."
            )

        karma_prompt = f"""Sen hem İlm-i Sîmâ (yüz okuma), hem İlm-i Nücûm (doğum haritası), \
hem de İlm-i Ebced (isim sayısı) geleneğine hâkim bir Osmanlı müneccim-feraset üstadısın.

Bu kişinin YÜZÜNÜ fotoğraftan gerçekten incele. Aşağıda da doğum haritasındaki \
gezegen yerleşimleri var:

{natal_text}
{time_note}{ebced_text}

Görevin: Yüzden okuduğun mizaç ile haritadaki gezegen yerleşimlerini (ve verilmişse isim \
ebcedini) TEK bir bütünsel kıraatte harmanlamak. Yüzdeki bir özelliğin haritadaki bir \
yerleşimle nasıl örtüştüğünü (veya gerilim oluşturduğunu) göster. DENGELİ ol: sadece güçlü \
yönleri değil, zaafları, iç çelişkileri ve gerilimleri de dürüstçe yaz. Yağcılık yapma; \
klasik müneccim üslubunda hem meziyeti hem gölgeyi söyle. Bu bir eğlence ve kültürel \
uygulamadır; kişiyi yıkmadan ama gerçekçi yaz.

Ayrıca brifing alanlarını da doldur: 'guclu_yanlar' (parlak yanlar), 'golge_yanlar' (zaaflar, \
dürüst ama kırıcı olmadan), ve 'dikkat_edilecekler' (bu mizaçla daha iyi anlaşmak için yapıcı \
tavsiyeler — 'şu kişiden sakın' gibi yargı değil). Sadece 'karma_kiraat' aracını çağırarak cevap ver."""

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

        # Hesaplanan haritayı ve ebced'i de geri gönder (arayüzde göstermek için)
        result["natal"] = natal
        if ebced:
            result["ebced"] = ebced
        return jsonify(result)

    except anthropic.APIError as e:
        return jsonify({"error": f"API hatası: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"Beklenmeyen hata: {str(e)}"}), 500


# ---- GÜNLÜK KIRAAT: her gün taze, kişiye özel yorum ----
GUNLUK_TOOL = {
    "name": "gunluk_kiraat",
    "description": "Kişiye ve güne özel kısa günlük kıraat döndürür.",
    "input_schema": {
        "type": "object",
        "properties": {
            "baslik": {"type": "string", "description": "Güne özel kısa, çarpıcı bir başlık (3-5 kelime)"},
            "kiraat": {"type": "string", "description": "Bugüne özel kıraat, 3-4 cümle, klasik ama sıcak üslup"},
            "tavsiye": {"type": "string", "description": "Bugün için tek cümlelik pratik tavsiye"},
            "ugurlu_sayi": {"type": "integer", "description": "Bugüne özel uğurlu sayı (1-99)"},
        },
        "required": ["baslik", "kiraat", "tavsiye", "ugurlu_sayi"],
    },
}


@app.route("/gunluk", methods=["POST"])
def gunluk():
    try:
        data = request.get_json()
        birth = data.get("birth", {})
        name = (data.get("name") or "").strip()
        today = (data.get("today") or "").strip()  # "2026-06-11" gibi, istemciden gelir

        try:
            year = int(birth.get("year"))
            month = int(birth.get("month"))
            day = int(birth.get("day"))
            hour = float(birth.get("hour", 12.0))
        except (TypeError, ValueError):
            return jsonify({"error": "Doğum bilgisi eksik."}), 400

        natal = compute_natal(year, month, day, hour)
        ebced = compute_ebced(name) if name else None

        natal_text = ""
        if natal:
            natal_text = "Doğum haritası: " + ", ".join(
                f"{p} {v['burc']}" for p, v in natal.items()
            )
        ebced_text = f" İsim ebcedi: {ebced['toplam']}." if ebced else ""
        kim = f"{name} adlı kişi" if name else "bu kişi"

        prompt = f"""Sen bir Osmanlı müneccim-feraset üstadısın. Bugünün tarihi: {today}.

{kim} için BUGÜNE özel kısa bir 'günün kıraati' yaz. {natal_text}{ebced_text}

Bugünün tarihini ve kişinin haritasını/ebcedini harmanla; her gün farklı, taze ve güne \
özgü bir yorum olsun (genel geçer değil). Sıcak, klasik ama anlaşılır bir üslup kullan. \
Bu eğlence ve kültürel bir uygulamadır. Sadece 'gunluk_kiraat' aracını çağırarak cevap ver."""

        message = client.messages.create(
            model=MODEL,
            max_tokens=800,
            tools=[GUNLUK_TOOL],
            tool_choice={"type": "tool", "name": "gunluk_kiraat"},
            messages=[{"role": "user", "content": prompt}],
        )
        result = None
        for block in message.content:
            if block.type == "tool_use" and block.name == "gunluk_kiraat":
                result = block.input
                break
        if result is None:
            return jsonify({"error": "Kıraat üretilemedi, tekrar dene."}), 502
        return jsonify(result)

    except anthropic.APIError as e:
        return jsonify({"error": f"API hatası: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"Beklenmeyen hata: {str(e)}"}), 500


# ---- EL SÎMASI: elin/parmakların ŞEKLİNDEN firâset okuması ----
EL_TOOL = {
    "name": "el_simasi",
    "description": "Elin ve parmakların ŞEKLİNDEN firâset okumasını döndürür.",
    "input_schema": {
        "type": "object",
        "properties": {
            "el_tipi": {
                "type": "string",
                "description": "Elin klasik tipi/arketipi (örn: 'Toprak eli', 'Ateş eli' gibi kısa bir nitelik)",
            },
            "ozellikler": {
                "type": "array",
                "description": "Elin 3-4 okuma yeri (avuç biçimi, parmak uzunluğu/oranı, başparmak, el yapısı)",
                "items": {
                    "type": "object",
                    "properties": {
                        "ad": {"type": "string"},
                        "yorum": {"type": "string"},
                    },
                    "required": ["ad", "yorum"],
                },
            },
            "kiraat": {
                "type": "string",
                "description": "Elin bütünsel firâset kıraati, 3-4 cümle, dengeli (güçlü + gölge), klasik üslup",
            },
        },
        "required": ["el_tipi", "ozellikler", "kiraat"],
    },
}

EL_PROMPT = """Sen firâset (ilm-i sîmâ) geleneğine hâkim bir üstadsın. Firâset yüz kadar \
bedenin diğer dış işaretlerini de okur; bunlardan biri elin ve parmakların ŞEKLİDİR \
(avucun biçimi, parmakların uzunluğu ve oranları, başparmağın yapısı, elin genel kuruluşu). \
DİKKAT: Bu avuç içi ÇİZGİSİ falı (el falı/kiromansi) DEĞİLDİR; sen çizgileri değil, elin \
ve parmakların biçimini/oranlarını okuyorsun.

Gönderilen el fotoğrafını gerçekten incele ve firâset perspektifinden oku. DENGELİ ol: \
hem güçlü yönleri hem zaafları söyle, yağcılık yapma. Eğer görselde el net görünmüyorsa \
bunu kıraatte nazikçe belirt. Bu eğlence ve kültürel bir uygulamadır; tıbbi/kesin iddia yok. \
Sadece 'el_simasi' aracını çağırarak cevap ver."""


@app.route("/el", methods=["POST"])
def el():
    try:
        data = request.get_json()
        image_b64 = data.get("image")
        media_type = data.get("mediaType", "image/jpeg")
        if not image_b64:
            return jsonify({"error": "El görseli bulunamadı"}), 400

        message = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            tools=[EL_TOOL],
            tool_choice={"type": "tool", "name": "el_simasi"},
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
                        {"type": "text", "text": EL_PROMPT},
                    ],
                }
            ],
        )
        result = None
        for block in message.content:
            if block.type == "tool_use" and block.name == "el_simasi":
                result = block.input
                break
        if result is None:
            return jsonify({"error": "El okuması üretilemedi, tekrar dene."}), 502
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
