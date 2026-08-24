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
import urllib.request
import urllib.error

from flask import Flask, request, jsonify, send_from_directory, Response
import anthropic

try:
    from guard import korumali, kaydet_kullanim, guard_durum  # noqa
    KALKAN = True
except ImportError:
    KALKAN = False
    # guard.py repoya yüklenmemiş. Uygulamayı çökertme — ama pahalı uçları da
    # korumasız açma. Site ayakta kalır, okuma uçları kapalı kalır.
    import logging
    logging.error("guard.py bulunamadi! Pahali uclar kapatildi. Dosyayi repoya ekle.")

    def korumali(uc, agir=True):
        def sar(fn):
            from functools import wraps

            @wraps(fn)
            def ic(*a, **kw):
                if not agir:
                    return fn(*a, **kw)
                return jsonify({
                    "error": "kalkan_yok",
                    "mesaj": "Üstat şu an kıraat yapamıyor. Kısa bir bakımdayız."
                }), 503
            return ic
        return sar

    def kaydet_kullanim(*a, **kw):
        return 0.0

    def guard_durum():
        return {"uyari": "guard.py yuklu degil"}

# Doğum haritası hesabı (Moshier efemerisi — dış veri/internet gerektirmez)
try:
    import swisseph as swe
    ASTRO_OK = True
except Exception:
    ASTRO_OK = False

# static_folder=None ÖNEMLİ: aksi halde Flask uygulama klasöründeki HER dosyayı
# kök adresten servis eder — app.py, guard.py, hatta .env dahil. Servis edilecek
# her dosyanın açık bir rotası olmalı.
app = Flask(__name__, static_folder=None)
# Aşırı büyük istek gövdesi ücretsiz plandaki işçiyi düşürebiliyor.
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB

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
        "Satürn": swe.SATURN, "Uranüs": swe.URANUS, "Neptün": swe.NEPTUNE,
        "Plüton": swe.PLUTO, "Kuzey Ay Düğümü": swe.TRUE_NODE,
    }

# Burç nitelikleri (element / nitelik) ve yöneticileri
ELEMENTS = ["Ateş", "Toprak", "Hava", "Su"] * 3
QUALITIES = ["Öncü", "Sabit", "Değişken"] * 4
SIGN_RULERS = ["Mars", "Venüs", "Merkür", "Ay", "Güneş", "Merkür",
               "Venüs", "Plüton", "Jüpiter", "Satürn", "Uranüs", "Neptün"]

# Ev anlamları — konu bazlı yorum için
HOUSE_MEANINGS = {
    1: "benlik, görünüş, ilk izlenim", 2: "para, kazanç, öz değer",
    3: "iletişim, kardeşler, yakın çevre", 4: "yuva, kök, aile",
    5: "aşk, haz, yaratıcılık, çocuk", 6: "gündelik düzen, iş rutini, hizmet",
    7: "ortaklık, evlilik, birebir ilişkiler", 8: "dönüşüm, ortak kaynaklar, derinlik",
    9: "inanç, uzak yolculuk, öğreti", 10: "kariyer, itibar, toplumdaki yer",
    11: "dostluk, topluluk, hedefler", 12: "içsel dünya, geri çekilme, gizli olan",
}

# Ana açılar: (derece, ad, orb)
ASPECTS = [
    (0, "Kavuşum", 8), (60, "Altmışlık", 4), (90, "Kare", 6),
    (120, "Üçgen", 6), (180, "Karşıt", 8),
]


def _house_of(lon, cusps):
    """Bir gezegenin hangi evde olduğunu bulur (360° dönüşünü de hesaba katar)."""
    for i in range(12):
        a, b = cusps[i], cusps[(i + 1) % 12]
        if a < b:
            if a <= lon < b:
                return i + 1
        else:  # 360°'yi geçen ev
            if lon >= a or lon < b:
                return i + 1
    return None


def _aspects_between(positions):
    """Gezegenler arası ana açıları bulur. positions: {ad: boylam}"""
    names = list(positions.keys())
    found = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            p1, p2 = names[i], names[j]
            if "Düğümü" in p1 or "Düğümü" in p2:
                continue
            diff = abs(positions[p1] - positions[p2]) % 360
            if diff > 180:
                diff = 360 - diff
            for angle, ad, orb in ASPECTS:
                if abs(diff - angle) <= orb:
                    found.append({
                        "gezegen1": p1, "gezegen2": p2, "aci": ad,
                        "sapma": round(abs(diff - angle), 1),
                    })
                    break
    return found


def _sign_of(lon):
    i = int(lon // 30) % 12
    return ZODIAC_TR[i], ZODIAC_AR[i], round(lon % 30, 1)


# Türkiye 81 il — yaklaşık enlem/boylam (yükselen ve ev hesabı için)
TR_CITIES = {
    "adana": (37.00, 35.32), "adıyaman": (37.76, 38.28), "afyonkarahisar": (38.76, 30.54),
    "ağrı": (39.72, 43.05), "amasya": (40.65, 35.83), "ankara": (39.93, 32.86),
    "antalya": (36.90, 30.70), "artvin": (41.18, 41.82), "aydın": (37.84, 27.84),
    "balıkesir": (39.65, 27.89), "bilecik": (40.14, 29.98), "bingöl": (38.88, 40.50),
    "bitlis": (38.40, 42.11), "bolu": (40.74, 31.61), "burdur": (37.72, 30.29),
    "bursa": (40.18, 29.07), "çanakkale": (40.16, 26.41), "çankırı": (40.60, 33.62),
    "çorum": (40.55, 34.95), "denizli": (37.78, 29.09), "diyarbakır": (37.91, 40.24),
    "edirne": (41.68, 26.56), "elazığ": (38.68, 39.22), "erzincan": (39.75, 39.50),
    "erzurum": (39.90, 41.27), "eskişehir": (39.78, 30.52), "gaziantep": (37.07, 37.38),
    "giresun": (40.91, 38.39), "gümüşhane": (40.46, 39.48), "hakkari": (37.58, 43.74),
    "hatay": (36.20, 36.16), "ısparta": (37.76, 30.55), "isparta": (37.76, 30.55),
    "mersin": (36.81, 34.64), "içel": (36.81, 34.64), "istanbul": (41.01, 28.98),
    "izmir": (38.42, 27.14), "kars": (40.60, 43.10), "kastamonu": (41.39, 33.78),
    "kayseri": (38.73, 35.49), "kırklareli": (41.74, 27.22), "kırşehir": (39.15, 34.16),
    "kocaeli": (40.77, 29.92), "izmit": (40.77, 29.92), "konya": (37.87, 32.48),
    "kütahya": (39.42, 29.98), "malatya": (38.35, 38.32), "manisa": (38.61, 27.43),
    "kahramanmaraş": (37.58, 36.93), "maraş": (37.58, 36.93), "mardin": (37.31, 40.74),
    "muğla": (37.22, 28.36), "muş": (38.74, 41.49), "nevşehir": (38.62, 34.71),
    "niğde": (37.97, 34.68), "ordu": (40.98, 37.88), "rize": (41.02, 40.52),
    "sakarya": (40.76, 30.38), "adapazarı": (40.76, 30.38), "samsun": (41.29, 36.33),
    "siirt": (37.93, 41.94), "sinop": (42.03, 35.15), "sivas": (39.75, 37.02),
    "tekirdağ": (40.98, 27.51), "tokat": (40.31, 36.55), "trabzon": (41.00, 39.72),
    "tunceli": (39.11, 39.55), "şanlıurfa": (37.17, 38.79), "urfa": (37.17, 38.79),
    "uşak": (38.68, 29.41), "van": (38.49, 43.41), "yozgat": (39.82, 34.81),
    "zonguldak": (41.46, 31.79), "aksaray": (38.37, 34.03), "bayburt": (40.26, 40.23),
    "karaman": (37.18, 33.22), "kırıkkale": (39.85, 33.52), "batman": (37.88, 41.13),
    "şırnak": (37.52, 42.46), "bartın": (41.64, 32.34), "ardahan": (41.11, 42.70),
    "ığdır": (39.92, 44.04), "yalova": (40.65, 29.28), "karabük": (41.20, 32.62),
    "kilis": (36.72, 37.12), "osmaniye": (37.07, 36.25), "düzce": (40.84, 31.16),
}
DEFAULT_COORD = (39.93, 32.86)  # Ankara (yer bilinmezse)


def _tr_utc_offset(year, month, day, hour):
    """Türkiye'nin o tarihteki UTC offset'ini (saat) yaklaşık verir.
    2016 Eyl'den beri sabit UTC+3. Öncesinde kışın +2, yazın +3 (DST).
    Tam tarihsel DST geçişleri karmaşıktır; pratikte yeterli bir yaklaşım kullanıyoruz."""
    if (year > 2016) or (year == 2016 and month >= 9):
        return 3.0
    # Kabaca: son hafta sonu mart -> son hafta sonu ekim arası yaz saati (+3), diğer zaman +2
    if 4 <= month <= 9:
        return 3.0
    if month == 3 and day >= 28:
        return 3.0
    if month == 10 and day < 28:
        return 3.0
    return 2.0


def compute_natal(year, month, day, hour=12.0, city=None):
    """Tam doğum haritası: gezegenler + yükselen + evler.
    city verilirse koordinat ve saat dilimi düzeltmesi uygulanır."""
    if not ASTRO_OK:
        return None

    # Saat dilimi düzeltmesi: yerel saat -> UTC
    offset = _tr_utc_offset(year, month, day, hour)
    ut_hour = hour - offset
    jd = swe.julday(year, month, day, ut_hour)
    flag = swe.FLG_MOSEPH | swe.FLG_SPEED

    result = {"gezegenler": {}}
    raw_lon = {}
    for name, pid in PLANETS_TR.items():
        res, _ = swe.calc_ut(jd, pid, flag)
        lon_p, speed = res[0], res[3]
        tr, ar, deg = _sign_of(lon_p)
        idx = int(lon_p // 30) % 12
        raw_lon[name] = lon_p
        result["gezegenler"][name] = {
            "burc": tr, "burc_ar": ar, "derece": deg,
            "element": ELEMENTS[idx], "nitelik": QUALITIES[idx],
            "retro": bool(speed < 0),
        }

    # Yükselen + evler (Placidus). Koordinat gerekir.
    lat, lon = DEFAULT_COORD
    if city:
        key = city.strip().lower()
        if key in TR_CITIES:
            lat, lon = TR_CITIES[key]
    try:
        cusps, ascmc = swe.houses(jd, lat, lon, b"P")  # Placidus
        asc, mc = ascmc[0], ascmc[1]
        atr, aar, adeg = _sign_of(asc)
        mtr, mar, mdeg = _sign_of(mc)
        result["yukselen"] = {"burc": atr, "burc_ar": aar, "derece": adeg}
        result["mc"] = {"burc": mtr, "burc_ar": mar, "derece": mdeg}
        result["evler"] = [
            dict(zip(("ev", "burc", "burc_ar", "derece"),
                     (i + 1,) + _sign_of(cusps[i])))
            for i in range(12)
        ]
        # ÖNEMLİ: her gezegenin hangi evde olduğu — konu bazlı yorumun temeli
        for name, lon_p in raw_lon.items():
            h = _house_of(lon_p, cusps)
            if h:
                result["gezegenler"][name]["ev"] = h
        # Harita yöneticisi (yükselen burcunun yöneticisi)
        result["harita_yoneticisi"] = SIGN_RULERS[int(asc // 30) % 12]
    except Exception:
        pass  # Ev hesabı başarısızsa gezegenlerle yetin

    # Gezegenler arası açılar
    result["acilar"] = _aspects_between(raw_lon)

    # Element ve nitelik dengesi (Ay Düğümü hariç)
    el_count, ni_count = {}, {}
    for name, v in result["gezegenler"].items():
        if "Düğümü" in name:
            continue
        el_count[v["element"]] = el_count.get(v["element"], 0) + 1
        ni_count[v["nitelik"]] = ni_count.get(v["nitelik"], 0) + 1
    result["element_dengesi"] = el_count
    result["nitelik_dengesi"] = ni_count

    return result


def natal_to_text(natal, detailed=True):
    """Haritayı LLM'e verilecek AYRINTILI metne çevirir."""
    if not natal:
        return ""
    lines = []
    for p, v in natal.get("gezegenler", {}).items():
        s = f"- {p}: {v['burc']} {v['derece']}°"
        if v.get("ev"):
            s += f", {v['ev']}. evde ({HOUSE_MEANINGS.get(v['ev'], '')})"
        if v.get("retro"):
            s += " [RETRO]"
        lines.append(s)
    if natal.get("yukselen"):
        y = natal["yukselen"]
        lines.append(f"- Yükselen (ASC): {y['burc']} {y['derece']}°")
    if natal.get("mc"):
        m = natal["mc"]
        lines.append(f"- Tepe Noktası (MC): {m['burc']} {m['derece']}° — kariyer ekseni")
    if natal.get("harita_yoneticisi"):
        lines.append(f"- Harita yöneticisi: {natal['harita_yoneticisi']}")
    if natal.get("element_dengesi"):
        el = ", ".join(f"{k}: {v}" for k, v in natal["element_dengesi"].items())
        lines.append(f"- Element dengesi: {el}")
    if natal.get("nitelik_dengesi"):
        ni = ", ".join(f"{k}: {v}" for k, v in natal["nitelik_dengesi"].items())
        lines.append(f"- Nitelik dengesi: {ni}")
    if detailed and natal.get("acilar"):
        ac = "; ".join(
            f"{a['gezegen1']} {a['aci']} {a['gezegen2']} ({a['sapma']}° sapma)"
            for a in natal["acilar"]
        )
        lines.append(f"- AÇILAR: {ac}")
    if detailed and natal.get("evler"):
        ev = ", ".join(f"{h['ev']}.ev {h['burc']}" for h in natal["evler"])
        lines.append(f"- Ev başlangıçları: {ev}")
    return "\n".join(lines)


def natal_for_topic(natal, houses, planets):
    """Belirli bir konu için haritanın ilgili kısmını çıkarır (aşk, para, kariyer...)."""
    if not natal:
        return ""
    lines = []
    gez = natal.get("gezegenler", {})
    # İlgili evlerde hangi gezegenler var
    for h in houses:
        icinde = [p for p, v in gez.items() if v.get("ev") == h]
        cusp = next((e for e in natal.get("evler", []) if e["ev"] == h), None)
        s = f"- {h}. ev ({HOUSE_MEANINGS.get(h, '')})"
        if cusp:
            s += f": {cusp['burc']} burcunda"
        s += f" — içindeki gezegenler: {', '.join(icinde) if icinde else 'boş'}"
        lines.append(s)
    # İlgili gezegenlerin durumu
    for p in planets:
        v = gez.get(p)
        if not v:
            continue
        s = f"- {p}: {v['burc']} {v['derece']}°"
        if v.get("ev"):
            s += f", {v['ev']}. evde"
        if v.get("retro"):
            s += " [RETRO]"
        ilgili = [a for a in natal.get("acilar", [])
                  if a["gezegen1"] == p or a["gezegen2"] == p]
        if ilgili:
            s += " | açıları: " + ", ".join(
                f"{a['aci']} {a['gezegen2'] if a['gezegen1'] == p else a['gezegen1']}"
                for a in ilgili
            )
        lines.append(s)
    return "\n".join(lines)


def natal_short(natal):
    """Kısa burç özeti (yükselen dahil)."""
    if not natal:
        return ""
    gez = natal.get("gezegenler", {})
    parts = [f"{p} {v['burc']}" for p, v in gez.items()]
    if natal.get("yukselen"):
        parts.append(f"Yükselen {natal['yukselen']['burc']}")
    return ", ".join(parts)


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
    print("Render'da Environment kısmına ANTHROPIC_API_KEY ekle.\n")

client = anthropic.Anthropic(api_key=API_KEY)

# Vision destekli model.
# Daha ucuz istersen: "claude-haiku-4-5-20251001"
MODEL = "claude-sonnet-4-6"
# Ucuz uçlar (günlük kıraat, eşleşme) buradan geçer — maliyetin 1/3'ü.
MODEL_UCUZ = "claude-haiku-4-5-20251001"


# SDK sürümüne göre temperature desteklenmeyebiliyor; ilk hatada kapatılır.
_SICAKLIK_DESTEKLI = True


def gemini_json(parts, schema, max_tokens, uc="?", model=None, sicaklik=1.0, _tekrar=False):
    """İçerik parçalarını modele gönderip yapılandırılmış JSON döndürür.
    parts: metin (str) ve/veya image_part() ile üretilmiş görsel bloklarından oluşan liste.
    schema: beklenen JSON şeması (dict).
    Tool use kullanır — çıktının her zaman geçerli yapıda gelmesini garantiler.
    (İsim geriye dönük uyumluluk için korundu.)
    """
    content = []
    for p in parts:
        content.append({"type": "text", "text": p} if isinstance(p, str) else p)

    tool = {
        "name": "yapilandirilmis_cevap",
        "description": "İstenen yapıda sonucu döndürür.",
        "input_schema": schema,
    }
    kw = {
        "model": model or MODEL,
        "max_tokens": max_tokens,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": "yapilandirilmis_cevap"},
        "messages": [{"role": "user", "content": content}],
    }
    if _SICAKLIK_DESTEKLI:
        kw["temperature"] = sicaklik

    try:
        message = client.messages.create(**kw)
    except TypeError as e:
        # Kurulu SDK sürümü bu parametreyi tanımıyor. Bir kez öğren, bir daha deneme.
        if "temperature" not in str(e):
            raise
        globals()["_SICAKLIK_DESTEKLI"] = False
        kw.pop("temperature", None)
        message = client.messages.create(**kw)
    # Gerçek token kullanımını maliyet sayacına yaz (tahmin değil).
    try:
        kaydet_kullanim(model or MODEL, message.usage.input_tokens,
                        message.usage.output_tokens, uc=uc)
    except Exception:
        pass
    # KRİTİK: token bütçesi dolduysa tool_use girdisi YARIM gelir ve alanların
    # bir kısmı sessizce boş kalır. Bunu yakalamadan döndürmek, kullanıcıya
    # boş bölümler göstermek demektir.
    if message.stop_reason == "max_tokens":
        # Bütçe yetmedi. Kullanıcıya hata göstermek yerine bir kez daha,
        # daha geniş bütçeyle dene. Bu, "yarıda kesildi" hatasının
        # kullanıcıya ulaşmasını engelleyen emniyet ağıdır.
        if not _tekrar:
            import logging
            logging.warning("Kiraat %s icin kesildi (%s token). Buyuk butceyle tekrar.",
                            uc, max_tokens)
            return gemini_json(parts, schema, int(max_tokens * 1.7),
                               uc=uc, model=model, sicaklik=sicaklik, _tekrar=True)
        raise ValueError("kiraat_yarida_kesildi")
    for block in message.content:
        if block.type == "tool_use" and block.name == "yapilandirilmis_cevap":
            return block.input
    return None


OLCUM_ADI = {
    "yuz_orani":   "Yüz genişliği / yüz uzunluğu",
    "ust_bolge":   "Üst bölge (alın) payı",
    "orta_bolge":  "Orta bölge (kaş–burun) payı",
    "alt_bolge":   "Alt bölge (burun–çene) payı",
    "elmacik_cene": "Elmacık genişliği / çene genişliği",
    "cene_acisi":  "Çene açısı (derece)",
    "goz_arasi":   "Gözler arası mesafe / göz genişliği",
    "kas_goz":     "Kaş–göz mesafesi / göz yüksekliği",
    "burun_orani": "Burun genişliği / yüz genişliği",
    "agiz_burun":  "Ağız genişliği / burun genişliği",
    "dudak_orani": "Üst dudak kalınlığı / alt dudak kalınlığı",
    "simetri":     "Simetri endeksi (1.00 = kusursuz)",
    "kas_farki":   "Kaş yüksekliği farkı (göz yüksekliği birimiyle; + ise sağ kaş yukarıda)",
}


def olcum_metni(olcum):
    """Tarayıcıda 68 yüz noktasından hesaplanan oranları prompta gömer.

    Neden önemli: bunlar modelin tahmini değil, ölçüm. Aynı fotoğraf her
    seferinde aynı sayıları verir. Kıraat bu sayılara dayanınca hem tutarlı
    olur hem de kullanıcının aynada doğrulayabileceği bir zemine oturur.
    """
    satir = []
    for anahtar, deger in olcum.items():
        ad = OLCUM_ADI.get(anahtar)
        if ad and isinstance(deger, (int, float)):
            satir.append(f"- {ad}: {deger}")
    if not satir:
        return ""
    return (
        "\n\n=== ÖLÇÜLEN DEĞERLER (yüz noktalarından otomatik hesaplandı) ===\n"
        + "\n".join(satir)
        + "\n\nBU SAYILAR SENİN TAHMİNİN DEĞİL, ÖLÇÜMDÜR — DOĞRUDUR.\n"
        "Kıraatini gözle gördüklerinle DEĞİL, öncelikle bu sayılarla kur. "
        "'gozlem' alanlarında somut ol: hangi oranın ne olduğunu söyle "
        "(örnek: 'Elmacık/çene oranı 1.28 — üst yüz alt yüzden belirgin geniş'). "
        "Sayılarla çelişen bir şey yazma. Bir ölçüm listede yoksa o yeri "
        "fotoğraftan oku, ama emin değilsen 'okunamayan' listesine yaz.\n"
        "Referans aralıklar: yüz oranı 0.70–0.80 dengeli sayılır; üç bölge "
        "birbirine yakınsa (her biri ~0.33) mizaç dengeli okunur; simetri 0.97 "
        "üstü yüksek, 0.93 altı belirgin asimetriktir; çene açısı 120° altı "
        "keskin, 140° üstü yumuşak çenedir.\n"
    )


def image_part(image_b64, media_type):
    """Tarayıcıdan gelen base64 görseli mesaj bloğuna çevirir."""
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": image_b64},
    }


def face_parts(data):
    """Ön/sağ/sol açılardan gelen yüz görsellerini etiketli parçalara çevirir.
    'image' (ön) zorunlu; 'imageRight' ve 'imageLeft' isteğe bağlıdır.
    Dönen: (parts_listesi, aci_sayisi) — hiç görsel yoksa (None, 0)."""
    front = data.get("image")
    if not front:
        return None, 0
    mt = data.get("mediaType", "image/jpeg")
    parts = ["=== ÖN CEPHE (yüzün karşıdan görünümü) ===",
             image_part(front, mt)]
    n = 1
    right = data.get("imageRight")
    if right:
        parts += ["=== SAĞ PROFİL (yüzün sağ yandan görünümü) ===",
                  image_part(right, data.get("mediaTypeRight", mt))]
        n += 1
    left = data.get("imageLeft")
    if left:
        parts += ["=== SOL PROFİL (yüzün sol yandan görünümü) ===",
                  image_part(left, data.get("mediaTypeLeft", mt))]
        n += 1
    return parts, n


MULTI_ANGLE_NOTE = """
ÇOK AÇILI OKUMA: Sana aynı kişinin birden fazla açıdan fotoğrafı verildi. Hepsi AYNI kişiye \
aittir, ayrı kişiler değil. Profil (yandan) görüntüler özellikle burun hattı ve kemeri, alın \
eğimi, çene çıkıntısı ve yüz derinliği için değerlidir; ön cephe ise simetri, gözler ve genel \
oranlar için. Okumanı tüm açılardan gördüklerini birleştirerek yap ve mümkün olduğunca \
profilden gelen bilgiyi de kullan."""


SIMA_PROMPT = """Sen İlm-i Sîmâ (fizyonomi) ve İlm-i Kef (el okuma) geleneğine hâkim bir \
Osmanlı üstadısın. Yüz hatlarından ve elden kişinin mizacını ve yolunun eğilimini okursun.

Gönderilen yüz fotoğraf(lar)ını GERÇEKTEN dikkatle incele. El fotoğrafı da verildiyse avuç \
içini ciddiyetle incele.

ŞU ALTI OKUMA YERİ İÇİN BİRER trait üret: 1. ALIN (الجَبْهَة) 2. KAŞLAR (الحَواجِب) \
3. GÖZLER (العُيُون) 4. BURUN (الأَنْف) 5. AĞIZ VE DUDAKLAR (الشِّفَاه) 6. ÇENE (الذَّقَن).
'name' Türkçe adı, 'arabic' Arapça karşılığı olsun.

ÖNCE KANIT, SONRA HÜKÜM — BU KURALIN İHLALİ EN BÜYÜK HATADIR:
Her okuma yerinde önce 'gozlem' alanını doldur. Buraya SADECE gözünle gördüğünü yaz: oran, \
asimetri, açı, mesafe, kalınlık, yön. Kişi telefonunu aynaya tutup 'evet, gerçekten öyle' \
diyebilmeli. Karakter yorumu bu alana GİRMEZ. Sonra 'hukum' alanında o gözlemin mizaç \
karşılığını tek cümlede söyle. 
BARNUM YASAĞI: Herkese uyan cümle kurmak bu uygulamada en ağır kusurdur. Şunlar YASAK: \
'hem içine dönük hem dışa dönüksün', 'potansiyelin var ama tam kullanmıyorsun', 'zaman zaman \
kendinden şüphe edersin', 'güçlü bir kişiliğin var'. Her cümle şu testi geçmeli: bu cümle \
başka birine söylensin, YANLIŞ olsun. Yanlış olamıyorsa cümle değersizdir, sil ve yeniden yaz.

DÜRÜSTLÜK — GÜVENİN KAYNAĞI: Fotoğrafta bir yer gölgede, kapalı, bulanık ya da açı yüzünden \
belirsizse UYDURMA. O yeri 'okunamayan' listesine yaz ve o trait'in gözlemini 'bu açıdan net \
seçilmiyor' diye dürüstçe kur. Emin olmadığını söylemek, yanlış hüküm vermekten çok daha \
değerlidir — kullanıcı senin uydurmadığını anladığında geri kalanına inanır.

'gelecek' alanı AYRI ve ZORUNLUDUR — gözlemi ve hükmü tekrar etmeden, 'bu hat seni ileride \
...e taşır' ya da 'bunu törpülemende fayda var' dilinde tek cümle. Bu alanı boş bırakma.

MANŞET: 'hukum_cumlesi' alanına en fazla 12 kelimelik tek bir cümle yaz. Bu cümle kullanıcının \
göreceği İLK şeydir ve ekran görüntüsü alıp paylaşacağı cümledir. Bir gerilim taşısın — iki \
zıt şeyi aynı anda söylesin. 'Karar veren ama kararını kimseye açmayan bir sîmâ.' gibi.

EL OKUMA (el fotoğrafı verildiyse): Elin tipini söyle ve ÜÇ ANA ÇİZGİYİ oku — Kalp Çizgisi \
(duygu yolu), Akıl Çizgisi (zihin yolu), Hayat Çizgisi (canlılık yolu). Fotoğrafta GERÇEKTEN \
görünene dayan; bir çizgi net seçilemiyorsa bunu dürüstçe söyle, uydurma. 'yorum' sadece \
gözlem (1 cümle); 'gelecek' alanı ayrı ve zorunlu, geleceğe dönük eğilim. El fotoğrafı \
YOKSA 'el' alanlarını boş string ve boş listeyle doldur.

DENGE: Hem parlak yönü hem gölgeyi söyle; gölgeyi yapıcı gelecek diliyle ver ('çabuk \
parlayabilirsin — ileride törpülemende fayda var' gibi). Kesin kehanet yok ('şu tarihte şu \
olacak' deme), eğilim dili var. Bu eğlence ve kültürel bir uygulamadır; tıbbi iddia yasak. \
Yalnızca istenen JSON yapısında cevap ver."""


# Tool use: çıktının her zaman geçerli yapıda gelmesini garantiler
SIMA_TOOL = {
    "name": "sima_analizi",
    "description": "İlm-i sîmâ yüz ve el analizini yapılandırılmış biçimde döndürür.",
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
                "description": "Altı okuma yeri (Alın, Kaşlar, Gözler, Burun, Ağız/Dudaklar, Çene)",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "arabic": {"type": "string"},
                        "gozlem": {
                            "type": "string",
                            "description": "SADECE fiziksel gözlem — kişi aynaya bakıp doğrulayabilmeli. "
                                           "Oran, asimetri, yön, açı, mesafe, kalınlık. Tek cümle. "
                                           "Mizaç/karakter YORUMU buraya YAZILMAZ. "
                                           "İyi: 'Sol kaşın ucu sağdakinden belirgin yukarıda, iç uçlar birbirine yakın.' "
                                           "Kötü: 'Kaşların kararlılığını gösteriyor.'",
                        },
                        "hukum": {
                            "type": "string",
                            "description": "Yukarıdaki gözlemin mizaç karşılığı. Tek cümle, gözlemi tekrar etme. "
                                           "'Zekisin', 'duygusalsın', 'hem içe hem dışa dönüksün' gibi HERKESE "
                                           "uyan cümleler YASAK — bu kişiye özgü, ayrımı keskin bir şey söyle.",
                        },

                        "gelecek": {
                            "type": "string",
                            "description": "ZORUNLU, TEK cümle, geleceğe dönük eğilim. Kalıp: 'Bu hat seni "
                                           "ileride ...e taşır' YA DA '...meylin var — bunu ileride "
                                           "törpülemende/güçlendirmende fayda var'. Gözlem cümlesini "
                                           "tekrar etme, yeni bir şey söyle.",
                        },
                        "intensity": {"type": "integer"},
                    },
                    "required": ["name", "arabic", "gozlem", "hukum",
                                 "gelecek", "intensity"],
                },
            },
            "el": {
                "type": "object",
                "description": "SADECE el fotoğrafı verildiyse doldur; yoksa alanları boş string bırak.",
                "properties": {
                    "el_tipi": {"type": "string", "description": "Elin klasik tipi (örn: 'Ateş eli')"},
                    "cizgiler": {
                        "type": "array",
                        "description": "Avuç çizgilerinin okunması: Kalp, Akıl (baş) ve Hayat çizgisi. "
                                       "Fotoğrafta gerçekten görüneni oku; net görünmüyorsa söyle.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ad": {"type": "string", "description": "Kalp Çizgisi / Akıl Çizgisi / Hayat Çizgisi"},
                                "yorum": {"type": "string", "description": "1 cümle: sadece gözlem (çizginin şekli/derinliği)."},
                                "gelecek": {
                                    "type": "string",
                                    "description": "ZORUNLU, tek cümle, geleceğe dönük eğilim. "
                                                   "'Bu çizgi ileride ...e işaret eder' kalıbında.",
                                },
                            },
                            "required": ["ad", "yorum", "gelecek"],
                        },
                    },
                    "kiraat": {"type": "string", "description": "Elin bütünsel kıraati, 2-3 cümle, geleceğe dönük"},
                },
                "required": ["el_tipi", "cizgiler", "kiraat"],
            },
            "hukum_cumlesi": {
                "type": "string",
                "description": "TEK cümle, en fazla 12 kelime, noktayla biter. Bu sîmânın özü. "
                               "Kişi bunu okuduğunda 'beni tarif etti' demeli — ama cümle HERKESE "
                               "uymamalı. Bir gerilim/çelişki taşısın. "
                               "İyi: 'Karar veren ama kararını kimseye açmayan bir sîmâ.' "
                               "Kötü: 'Güçlü ve duyarlı bir kişiliğin var.'",
            },
            "okunamayan": {
                "type": "array",
                "description": "Bu fotoğraftan GÜVENLE okunamayan yerler. Açı, gölge, saç/gözlük "
                               "kapatması, çözünürlük yüzünden emin olamadığın her yeri buraya yaz. "
                               "Hiçbir şey engellemiyorsa boş liste bırak. UYDURMA — emin değilsen "
                               "yorum üretmek yerine buraya yazmak DAHA DEĞERLİDİR.",
                "items": {"type": "string"},
            },
            "overall": {
                "type": "string",
                "description": "Bütünsel kıraat, 3-4 cümle, klasik Osmanlı üslubu; yüz (ve varsa el) "
                               "birlikte, geleceğe dönük eğilim diliyle kapanır.",
            },
        },
        "required": ["hukum_cumlesi", "dominantTrait", "dominantTraitTR", "traits",
                     "okunamayan", "el", "overall"],
    },
}


@app.errorhandler(Exception)
def _hata_json(e):
    """Flask varsayılan olarak HTML hata sayfası döner; istemci onu ayrıştıramaz
    ve kullanıcı 'beklenmeyen cevap' görür. Her hatayı JSON'a çeviriyoruz."""
    from werkzeug.exceptions import HTTPException
    kod = e.code if isinstance(e, HTTPException) else 500
    mesaj = {
        413: "Gönderdiğin görseller çok büyük. Daha küçük bir fotoğraf dene.",
        404: "Böyle bir sayfa yok.",
        429: "Çok sık istek geldi. Biraz bekle.",
    }.get(kod, "Üstat şu an cevap veremedi. Birazdan tekrar dene.")
    return jsonify({"error": "sunucu", "mesaj": mesaj}), kod


SURUM = "nur-16"   # arayüz sürümü — dağıtımın gerçekten yenilendiğini doğrulamak için


@app.route("/")
def index():
    # index.html ASLA önbelleğe alınmasın. Aksi halde kullanıcı (özellikle iOS
    # Safari) günlerce eski arayüzü görür ve "deploy oldu mu?" belirsiz kalır.
    r = send_from_directory(".", "index.html")
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["X-Sima-Surum"] = SURUM
    return r


@app.route("/_surum")
def _surum():
    """Tek bakışta sağlık kontrolü. Tarayıcıdan aç: /_surum"""
    return jsonify({
        "surum": SURUM,
        "kalkan": KALKAN,
        "ses_anahtari": bool(ELEVEN_API_KEY),
        "ses_kimligi": bool(ELEVEN_VOICE_ID),
        "durum": "hazir" if KALKAN else "guard.py EKSIK - okuma uclari kapali",
    })


# ---- PWA: ana ekrana eklenebilirlik ----------------------------------------
VARLIKLAR = {
    "ikon-192.png", "ikon-512.png", "ikon-maskable.png",
    "apple-touch-icon.png", "favicon.png",
}


@app.route("/varlik/<ad>")
def varlik(ad):
    if ad not in VARLIKLAR:
        return "", 404
    r = send_from_directory("varlik", ad)
    r.headers["Cache-Control"] = "public, max-age=604800"
    return r


@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": "İlm-i Sîmâ",
        "short_name": "Sîmâ",
        "description": "Yüz hatlarından mizaç kıraati — doğum haritası, ebced ve el okumasıyla.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0A0908",
        "theme_color": "#0A0908",
        "lang": "tr",
        "dir": "ltr",
        "categories": ["lifestyle", "entertainment"],
        "icons": [
            {"src": "/varlik/ikon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/varlik/ikon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/varlik/ikon-maskable.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    })


# Belge için ağ önceliği (eski arayüz takılı kalmasın), varlıklar için önbellek.
SW_JS = """
const AD = 'sima-%s';
const VARLIK = ['/varlik/ikon-192.png', '/varlik/apple-touch-icon.png', '/varlik/favicon.png'];

self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(AD).then(c => c.addAll(VARLIK)).catch(() => {}));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(k =>
    Promise.all(k.filter(x => x !== AD).map(x => caches.delete(x)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.method !== 'GET') return;
  const u = new URL(r.url);
  if (u.origin !== location.origin) return;

  // Belge: her zaman ağdan. Ağ yoksa son çare önbellek.
  if (r.mode === 'navigate') {
    e.respondWith(fetch(r).catch(() => caches.match('/') || new Response(
      '<h1 style="font-family:serif;color:#C9A84C;background:#0A0908;padding:40px">' +
      'Bağlantı yok. Üstat çevrimdışı okuyamaz.</h1>',
      { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
    return;
  }
  // Varlıklar: önce önbellek.
  if (u.pathname.startsWith('/varlik/')) {
    e.respondWith(caches.match(r).then(c => c || fetch(r).then(res => {
      const kopya = res.clone();
      caches.open(AD).then(ch => ch.put(r, kopya)).catch(() => {});
      return res;
    })));
  }
});
""" % SURUM


@app.route("/sw.js")
def sw():
    return Response(SW_JS, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.route("/analyze", methods=["POST"])
@korumali("analyze")
def analyze():
    try:
        data = request.get_json()
        parts, n_angles = face_parts(data)

        if not parts:
            return jsonify({"error": "Görsel bulunamadı"}), 400

        # El fotoğrafı (isteğe bağlı) — avuç içi çizgi okuması için
        hand = data.get("imageHand")
        if hand:
            parts += ["=== EL (avuç içi — çizgi okuması için) ===",
                      image_part(hand, data.get("mediaTypeHand", "image/jpeg"))]

        prompt = SIMA_PROMPT + (MULTI_ANGLE_NOTE if n_angles > 1 else "")
        olcum = data.get("olcum")
        if isinstance(olcum, dict) and olcum:
            prompt += olcum_metni(olcum)
        result = gemini_json(parts + [prompt], SIMA_TOOL["input_schema"], 3200,
                             uc="analyze", sicaklik=0.25)

        if result is None:
            return jsonify({"error": "Model analiz üretmedi, tekrar dene."}), 502

        return jsonify(result)

    except ValueError as e:
        if "yarida_kesildi" in str(e):
            return jsonify({"error": "kesildi",
                            "mesaj": "Üstat kıraatini tamamlayamadı. Tekrar dene."}), 502
        return jsonify({"error": str(e)}), 500
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
                "description": "Bütünsel karma kıraati, 4-5 cümle, klasik Osmanlı üslubu ama anlaşılır. Yüzle haritayı iç içe geçir; genel geçer değil, bu haritaya özgü yaz.",
            },
            "belirgin_yerlesimler": {
                "type": "array",
                "description": "Bu haritanın EN ÇARPICI 2-3 imzası (harita yöneticisi, en dar açı, bir evde yığılma, baskın/eksik element, retro gezegen gibi). Sıradan olanı değil, bu haritayı DİĞERLERİNDEN AYIRANI seç.",
                "items": {
                    "type": "object",
                    "properties": {
                        "yerlesim": {"type": "string", "description": "Teknik yerleşim (örn: 'Ay 12. evde, Satürn ile kare')"},
                        "anlami": {"type": "string", "description": "Bu yerleşimin bu kişide somut olarak neye dönüştüğü, 2 cümle"},
                    },
                    "required": ["yerlesim", "anlami"],
                },
            },
            "hayat_alanlari": {
                "type": "array",
                "description": "Haritanın konu konu okunuşu. ŞU DÖRT ALANIN HER BİRİ İÇİN birer madde yaz: Aşk (5/7. ev, Venüs, Ay), Para (2/8. ev, Jüpiter, Venüs), Kariyer (6/10. ev, MC, Satürn), Canlılık (1. ev, Mars, Güneş — SADECE enerji/tempo, tıbbi iddia YASAK).",
                "items": {
                    "type": "object",
                    "properties": {
                        "alan": {"type": "string", "description": "Alan adı (yukarıdaki dörtten biri)"},
                        "yorum": {"type": "string", "description": "Bu alandaki GÖZLEM, 2 cümle. Gelecek/tavsiye buraya yazma, ayrı alanda. Hem imkânı hem zorluğu söyle."},
                        "dayanak": {"type": "string", "description": "Bu yorumu dayandırdığın SOMUT yerleşim (örn: 'Venüs 8. evde, Plüton ile kavuşum')"},
                        "yol": {"type": "string", "description": "ZORUNLU, tek cümle: bu alanda önündeki yol/eğilim. 'Önümüzdeki dönemde ...' ya da '...meylin var, bunu değerlendirmende fayda var' kalıbında. yorum'u tekrar etme."},
                    },
                    "required": ["alan", "yorum", "dayanak", "yol"],
                },
            },
            "guclu_yanlar": {
                "type": "array",
                "description": "3 güçlü yan. Her biri: gözlem + ZORUNLU geleceğe dönük ikinci cümle.",
                "items": {
                    "type": "object",
                    "properties": {
                        "ozellik": {"type": "string", "description": "Kısa gözlem, parantez içinde dayanak (örn: 'Baskı altında soğukkanlı kalır (Satürn 1. evde)')"},
                        "gelecek": {"type": "string", "description": "ZORUNLU tek cümle: 'Bu seni ileride ...de öne çıkarır' kalıbında."},
                    },
                    "required": ["ozellik", "gelecek"],
                },
            },
            "golge_yanlar": {
                "type": "array",
                "description": "3 gölge yan. Her biri: dürüst gözlem + ZORUNLU YAPICI GELECEK ikinci cümle.",
                "items": {
                    "type": "object",
                    "properties": {
                        "ozellik": {"type": "string", "description": "Kısa gözlem, parantez içinde dayanak (örn: 'Yakınlıkta kontrolü bırakmakta zorlanır (Ay-Plüton karesi)')"},
                        "gelecek": {"type": "string", "description": "ZORUNLU tek cümle: 'Çabuk parlayabilirsin — bunu ileride törpülemende fayda var' kalıbında yapıcı tavsiye."},
                    },
                    "required": ["ozellik", "gelecek"],
                },
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
        "required": ["baslik", "kopruler", "kiraat", "belirgin_yerlesimler",
                     "hayat_alanlari", "guclu_yanlar", "golge_yanlar", "dikkat_edilecekler"],
    },
}


@app.route("/karma", methods=["POST"])
@korumali("karma")
def karma():
    try:
        data = request.get_json()
        # Karma'nın ağırlığı haritada; yüz için tek (ön) görsel yeterli.
        # Çok açılı gönderim burada bellek ve süre maliyetini gereksiz artırıyor.
        karma_parts, karma_angles = face_parts({
            "image": data.get("image"),
            "mediaType": data.get("mediaType", "image/jpeg"),
        })
        birth = data.get("birth", {})  # {year, month, day, hour}

        if not karma_parts:
            return jsonify({"error": "Görsel bulunamadı"}), 400

        # Doğum haritasını hesapla
        try:
            year = int(birth.get("year"))
            month = int(birth.get("month"))
            day = int(birth.get("day"))
            hour = float(birth.get("hour", 12.0))
        except (TypeError, ValueError):
            return jsonify({"error": "Doğum tarihi eksik veya hatalı."}), 400

        natal = compute_natal(year, month, day, hour, city=birth.get("city"))
        if natal is None:
            return jsonify({"error": "Doğum haritası modülü kullanılamıyor."}), 500

        # Ebced (isim verildiyse)
        name = (data.get("name") or "").strip()
        ebced = compute_ebced(name) if name else None

        # Haritayı okunabilir metne çevir
        natal_text = natal_to_text(natal)
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
hem de İlm-i Ebced (isim sayısı) geleneğine hâkim bir Osmanlı müneccim-feraset üstadısın. \
Sıradan bir burç yorumcusu değilsin; haritayı teknik olarak okuyup insan diline çeviren bir ustasın.

Bu kişinin YÜZÜNÜ fotoğraftan gerçekten incele. Aşağıda da doğum haritasının AYRINTILI dökümü var \
(gezegenlerin burçları, dereceleri, EVLERİ, açıları, element/nitelik dengesi, harita yöneticisi):

{natal_text}
{time_note}{ebced_text}

Görevin: Yüzden okuduğun mizaç ile haritadaki yerleşimleri (ve verilmişse isim ebcedini) \
harmanlayıp KONU KONU derinlemesine bir kıraat yazmak.

DERİNLİK KURALLARI — bunlara uymazsan iş yüzeysel kalır:
- Her yorumu SOMUT bir yerleşime dayandır: hangi gezegen, hangi burç, KAÇINCI EV, hangi açı. \
'dayanak' alanlarını gerçekten doldur. Dayanağı olmayan cümle kurma.
- EVLER en önemli katmandır. Burçlar 'nasıl'ı, evler 'nerede/hangi konuda'yı söyler. \
Konu bazlı yorumu ev yerleşimlerinden çıkar; sadece güneş burcuyla konuşma.
- AÇILARI kullan: kavuşum/kare/karşıt gerilim ve yoğunluk üretir, üçgen/altmış akış üretir. \
En dar sapmalı açılar en baskın olanlardır — onları öne çıkar.
- 'Duygusalsın', 'zeki birisin', 'hem güçlü hem hassassın' gibi HERKESE UYAN cümleler yasak. \
Bunun yerine ayrımı keskin şeyler yaz: hangi durumda ne yapar, neyi erteler, hangi baskı altında \
çatlar, hangi ilişki türünde tıkanır, parayı neye harcar, hangi işte söner.
- ÇELİŞKİLERİ göster: haritada birbirine ters düşen yerleşimler varsa (örn. Yükselen atak ama Ay \
çekingen) bunu söyle. Asıl derinlik çelişkidedir. Aynısını yüz ile harita arasında da yap.
- Boş evleri sorun sayma; o evin başlangıç burcunun yöneticisine bak.

GELECEK DİLİ — HER BÖLÜMDE ZORUNLU: Bu okuma geçmişi değil YOLU anlatır. 'guclu_yanlar' ve \
'golge_yanlar' artık {{ozellik, gelecek}} nesneleridir — 'gelecek' alanını BOŞ BIRAKMA, her \
zaman doldur. Aynı şekilde her hayat alanının 'yol' alanı da zorunludur ve 'yorum'u tekrar \
etmeden yeni, ileriye dönük bir şey söyler.

KISALIK: Uzun paragraf yok. Kiraat 4-5 cümle, her alan 'yorum' 2 cümle + 'yol' 1 cümle, \
madde 'ozellik' 1 cümle + 'gelecek' 1 cümle. Okuyan yorulmamalı.

DENGE: Yağcılık yapma; hem meziyeti hem gölgeyi söyle ama kişiyi yıkma. Kesin kehanet \
('şu tarihte şu olacak') ve tıbbi iddia YASAK — Canlılık alanı yalnızca enerji/tempo konuşur.

'dikkat_edilecekler' alanı: bu mizaçla daha iyi anlaşmak için yapıcı notlar — 'şu kişiden sakın' \
gibi yargı DEĞİL. Yalnızca istenen JSON yapısında cevap ver."""

        result = gemini_json(
            karma_parts + [karma_prompt + (MULTI_ANGLE_NOTE if karma_angles > 1 else "")],
            KARMA_TOOL["input_schema"],
            4000,
            uc="karma",
            sicaklik=0.25,
        )

        if result is None:
            return jsonify({"error": "Model kıraat üretmedi, tekrar dene."}), 502

        # Hesaplanan haritayı ve ebced'i de geri gönder (arayüzde göstermek için)
        # Frontend uyumu: gezegenleri üst düzeyde, yükselen/evleri ayrı alanda ver
        natal_out = dict(natal.get("gezegenler", {}))
        if natal.get("yukselen"):
            natal_out["Yükselen"] = natal["yukselen"]
        result["natal"] = natal_out
        if natal.get("evler"):
            result["evler"] = natal["evler"]
        if natal.get("mc"):
            result["mc"] = natal["mc"]
        if natal.get("acilar"):
            result["acilar"] = natal["acilar"]
        if natal.get("element_dengesi"):
            result["element_dengesi"] = natal["element_dengesi"]
        if natal.get("harita_yoneticisi"):
            result["harita_yoneticisi"] = natal["harita_yoneticisi"]
        if ebced:
            result["ebced"] = ebced
        return jsonify(result)

    except ValueError as e:
        if "yarida_kesildi" in str(e):
            return jsonify({"error": "kesildi",
                            "mesaj": "Kıraat tamamlanamadı. Tekrar dene."}), 502
        return jsonify({"error": str(e)}), 500
    except anthropic.APIError as e:
        return jsonify({"error": "api", "mesaj": f"Bağlantı hatası: {str(e)[:120]}"}), 502
    except Exception as e:
        return jsonify({"error": "genel", "mesaj": f"Beklenmeyen hata: {str(e)[:120]}"}), 500


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
@korumali("gunluk", agir=False)
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

        natal = compute_natal(year, month, day, hour, city=birth.get("city"))
        ebced = compute_ebced(name) if name else None

        natal_text = ""
        if natal:
            natal_text = "Doğum haritası: " + natal_short(natal)
        ebced_text = f" İsim ebcedi: {ebced['toplam']}." if ebced else ""
        kim = f"{name} adlı kişi" if name else "bu kişi"

        prompt = f"""Sen bir Osmanlı müneccim-feraset üstadısın. Bugünün tarihi: {today}.

{kim} için BUGÜNE özel kısa bir 'günün kıraati' yaz. {natal_text}{ebced_text}

Bugünün tarihini ve kişinin haritasını/ebcedini harmanla; her gün farklı, taze ve güne \
özgü bir yorum olsun (genel geçer değil). Sıcak, klasik ama anlaşılır bir üslup kullan. \
Bu eğlence ve kültürel bir uygulamadır. Yalnızca istenen JSON yapısında cevap ver."""

        result = gemini_json([prompt], GUNLUK_TOOL["input_schema"], 800,
                             uc="gunluk", model=MODEL_UCUZ)
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
Yalnızca istenen JSON yapısında cevap ver."""


@app.route("/eslesme", methods=["POST"])
@korumali("eslesme")
def eslesme():
    try:
        data = request.get_json()
        img1 = data.get("image1")
        img2 = data.get("image2")
        mt1 = data.get("mediaType1", "image/jpeg")
        mt2 = data.get("mediaType2", "image/jpeg")
        if not img1 or not img2:
            return jsonify({"error": "İki yüz görseli de gerekli."}), 400

        # İsteğe bağlı doğum haritaları
        birth1 = data.get("birth1") or {}
        birth2 = data.get("birth2") or {}

        def natal_of(b):
            try:
                return compute_natal(int(b["year"]), int(b["month"]), int(b["day"]),
                                     float(b.get("hour", 12.0)), city=b.get("city"))
            except (TypeError, ValueError, KeyError):
                return None

        natal1 = natal_of(birth1)
        natal2 = natal_of(birth2)

        astro_block = ""
        if natal1 or natal2:
            astro_block = "\n\nİKİ KİŞİNİN DOĞUM HARİTALARI (sinastri/uyum için bunları da harmanla):\n"
            if natal1:
                astro_block += f"- Birinci kişi: {natal_short(natal1)}\n"
            if natal2:
                astro_block += f"- İkinci kişi: {natal_short(natal2)}\n"
            astro_block += ("Yüz okumasıyla harita uyumunu birlikte değerlendir; burçların "
                            "ve yükselenlerin birbirini nasıl tamamladığını ya da gerdiğini de yorumla.")

        result = gemini_json(
            [
                "=== BİRİNCİ KİŞİ (bu fotoğraf birinci kişiye aittir) ===",
                image_part(img1, mt1),
                "=== İKİNCİ KİŞİ (bu fotoğraf ikinci kişiye aittir) ===",
                image_part(img2, mt2),
                ESLESME_PROMPT + astro_block,
            ],
            ESLESME_TOOL["input_schema"],
            1900,
            uc="eslesme",
            model=MODEL_UCUZ,
            sicaklik=0.4,
        )
        if result is None:
            return jsonify({"error": "Eşleşme üretilemedi, tekrar dene."}), 502
        # Haritaları da döndür (frontend göstermek isterse)
        if natal1:
            result["natal1"] = natal1
        if natal2:
            result["natal2"] = natal2
        return jsonify(result)

    except anthropic.APIError as e:
        return jsonify({"error": f"API hatası: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"Beklenmeyen hata: {str(e)}"}), 500


# ---- GELECEK KIRAATİ: aşk, bereket, kariyer, canlılık ----
def _tema_schema(konu, aciklama, ornek_isaret):
    return {
        "type": "object",
        "properties": {
            "baslik": {"type": "string", "description": f"{konu} için kısa çarpıcı başlık (3-5 kelime). Sadece {konu} temasına dair olsun."},
            "kiraat": {"type": "string", "description": f"SADECE {konu} hakkında: {aciklama} 6-8 cümle. Diğer temalara (aşk/para/kariyer/canlılık) girme, yalnızca {konu} yaz."},
            "isaret": {"type": "string", "description": f"Bu {konu} okumasını dayandırdığın somut yüz hattı (örn: '{ornek_isaret}')"},
            "tavsiye": {"type": "string", "description": f"{konu} için tek cümlelik yapıcı tavsiye"},
        },
        "required": ["baslik", "kiraat", "isaret", "tavsiye"],
    }


GELECEK_TOOL = {
    "name": "gelecek_kiraati",
    "input_schema": {
        "type": "object",
        "properties": {
            "ask": _tema_schema(
                "AŞK ve GÖNÜL",
                "Bağlanma biçimi, ilişkide güçlü yanı ve düştüğü tuzak, nasıl sevildiğini anlar.",
                "dudakların dolgunluğu, bakışın yumuşaklığı",
            ),
            "bereket": _tema_schema(
                "PARA ve BEREKET",
                "Para ile ilişkisi, kazanma ve harcama eğilimi, rızık ve bolluk yolu, cömertlik/tutumluluk.",
                "çenenin genişliği, burun kanatları",
            ),
            "kariyer": _tema_schema(
                "KARİYER ve İŞ",
                "Hangi işte parlar, hangi ortamda söner, ustalık yolu, otoriteyle ilişkisi, hangi bedeli öder.",
                "alnın genişliği, kaşların kararlılığı",
            ),
            "canlilik": _tema_schema(
                "CANLILIK ve TEMPO",
                "Enerji temposu, dinlenme ihtiyacı, kendini yıprattığı alışkanlık. SADECE yaşam temposu ve "
                "mizaç; hastalık, teşhis, organ veya tıbbi durumdan ASLA söz etme.",
                "gözlerin canlılığı, ten tonu",
            ),
            "muhur": {
                "type": "string",
                "description": "Dört temayı bağlayan tek cümlelik veciz kapanış",
            },
        },
        "required": ["ask", "bereket", "kariyer", "canlilik", "muhur"],
    },
}

GELECEK_PROMPT = """Sen İlm-i Sîmâ (firâset) geleneğine hâkim bir Osmanlı üstadısın. \
Gönderilen yüzü gerçekten dikkatle incele ve bu kişinin YOLU üzerine dört AYRI başlıkta kıraat yaz: \
Aşk, Bereket (para/rızık), Kariyer (iş), Canlılık (tempo).

>>> EN ÖNEMLİ KURAL — DÖRT BAŞLIK BİRBİRİNDEN TAMAMEN FARKLI OLACAK <<<
Aşk yalnızca gönül/ilişki konuşur. Bereket yalnızca para/rızık konuşur. Kariyer yalnızca iş/meslek \
konuşur. Canlılık yalnızca enerji/tempo konuşur. Bir başlığın metnini başka başlığa KOPYALAMA, \
yakınına bile getirme. Dört 'kiraat' alanı okununca 'bunlar aynı şeyi söylemiş' hissi ASLA \
uyanmamalı. Her başlıkta o konuya özgü farklı yüz hattını dayanak göster (aşkta dudak/bakış, \
bereket'te çene/burun, kariyerde alın/kaş, canlılıkta göz canlılığı/ten). Aynı yüz hattını iki \
başlıkta tekrar kullanma.

ÜSLUP VE ÇERÇEVE:
- Her okumayı gördüğün SOMUT bir yüz hattına ve (verilmişse) haritadaki SOMUT bir yerleşime \
dayandır ('isaret' alanında söyle). Genel geçer fal cümleleri yazma.
- YÜZEYSELLİK YASAK. 'duygusal bir yapın var', 'sevgiyi önemsersin', 'çalışkansın' gibi herkese \
uyan cümleler kurma. Kişiye özgü, ayrımı keskin şeyler yaz: hangi tip insana çekilir, hangi anda \
geri çekilir, hangi hatayı tekrar eder, neyi geciktirir, hangi ortamda parlar, hangi bedeli öder.
- Harita verildiyse gezegenin burcunu söylemekle yetinme; hangi EVDE olduğunu ve AÇILARINI yorumun \
içine ör (örn. 'Venüs 7. evde ama Satürn kare' → bağlanma isteği ile mesafe ihtiyacının çatışması).
- Kesin kehanet değil, MİZACIN EĞİLİMİ. 'Şu tarihte şu olacak' deme; 'bu mizaç şuna meyleder, \
önünü açmak için şunu yapar' dilini kullan.
- DENGELİ ol: her başlıkta hem parlak yanı hem düşülen tuzağı söyle. Yağcılık yapma.

MUTLAK SINIR — CANLILIK: Yalnızca yaşam temposu, enerji, dinlenme ve mizaç dengesi. Hastalık adı \
verme, teşhis koyma, organ/rahatsızlık ima etme, tıbbi tavsiye verme. Sağlık aracı değildir.

Bu eğlence ve kültürel bir uygulamadır. Yalnızca istenen JSON yapısında cevap ver."""


# ---- SES: ElevenLabs ile doğal Türkçe erkek sesi ----
ELEVEN_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
# Varsayılan: ElevenLabs'in bilinen "Adam" sesi (derin, İngilizce doğal ama çok dilli
# modelle Türkçe de konuşur). Kendi seçtiğin Türkçe sesin varsa Render'da
# ELEVENLABS_VOICE_ID değişkenini onunla değiştir — sonucu ciddi iyileştirir.
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")


SOR_TOOL = {
    "name": "ustat_cevabi",
    "description": "Üstadın cevabı.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cevap": {
                "type": "string",
                "description": "Üstadın cevabı. 2-4 cümle, en fazla 70 kelime. Klasik ama "
                               "anlaşılır Türkçe; ağdalı olmadan vakur. Kişiye 'sen' diye hitap et. "
                               "ELİNDEKİ KIRAATE DAYAN — sorulan şeyi onun sîmâsıyla ilişkilendir. "
                               "Kıraatte olmayan bir şey uydurma; bilmiyorsan bilmediğini söyle.",
            },
            "dokunus": {
                "type": "string",
                "description": "İsteğe bağlı tek cümlelik kapanış — bir soru ya da düşündürücü bir "
                               "not. Yoksa boş bırak.",
            },
        },
        "required": ["cevap"],
    },
}

SOR_PROMPT = """Sen İlm-i Sîmâ'nın üstadısın. Karşındaki kişinin yüzünü zaten okudun; \
kıraatin aşağıda. Şimdi sana bir soru soruyor.

ÜSLUP: Vakur, sakin, az konuşan bir üstat. Klasik ama anlaşılır Türkçe — ağdalı değil. \
Asla "yapay zekâyım", "model", "sistem" gibi sözler kullanma. Fal bakan bir şovmen de \
değilsin; ölçen, tartan, gerektiğinde susan bir kişisin.

HARİTA VARSA MUTLAKA KULLAN: Elindeki kıraatte doğum haritası bölümü varsa cevabını \
sadece yüze değil, yüz İLE haritayı birleştirerek kur. Somut ol: hangi gezegenin hangi \
burçta/evde olduğunu söyle ve bunun yüzdeki hangi hatla örtüştüğünü göster. \
Örnek kalıp: "Çenendeki genişlik ile Satürn'ün onuncu evdeki duruşu aynı şeyi söylüyor: geç \
ama kalıcı yükseliş." Ebced değeri varsa uygun düştüğü yerde an.

GELECEĞE DÖNÜK KONUŞ: Kişi "ne olacak" diye sorduğunda geçmişi özetleyip bırakma. Eğilim \
dilinde ilerisi hakkında konuş: "önündeki dönemde", "bu yıl", "yaklaşan yıllarda". Ama \
KESİN OLAY söyleme — meyli söyle, akıbeti değil. "Şu tarihte şu olacak" yasak; \
"bu dönemde şuna meyilli olursun, dikkat edersen şuraya varır" serbest. Mümkünse \
kişinin elindeki bir tutamağı göster: neyi yaparsa meyil lehine döner.

SINIRLAR — bunlar sarsılmaz:
- Elindeki kıraatte olmayan bir şeyi uydurma. Bilmiyorsan "sîmân bunu söylemiyor" de.
- Tıbbi, hukuki ya da mali tavsiye verme. Sorulursa kişiyi ehline yönlendir.
- Ölüm, hastalık, felaket kehanetinde bulunma. Sorulursa reddet: sîmâ meyli gösterir, akıbeti değil.
- Kimsenin yerine karar verme. Yol gösterirsin, emir vermezsin.
- Kısa konuş. En fazla 70 kelime.

Soru sîmâyla hiç ilgisiz bir şeyse (hava durumu, kod yazma, ödev) nazikçe reddet ve \
"ben yalnızca sîmâdan okuduğumu söylerim" de."""


@app.route("/sor", methods=["POST"])
@korumali("sor", agir=False)
def sor():
    """Üstatla serbest sohbet. Kıraat bağlamına sıkı sıkıya bağlı."""
    try:
        data = request.get_json() or {}
        soru = (data.get("soru") or "").strip()
        if not soru:
            return jsonify({"error": "bos", "mesaj": "Bir şey sormadın."}), 400
        if len(soru) > 400:
            soru = soru[:400]

        baglam = (data.get("baglam") or "").strip()[:2500]
        gecmis = data.get("gecmis") or []

        parcalar = [SOR_PROMPT]
        if baglam:
            parcalar.append("=== BU KİŞİNİN KIRAATİ ===\n" + baglam)
        else:
            parcalar.append("=== UYARI ===\nBu kişinin sîmâsını henüz okumadın. "
                            "Önce sîmâsına bakman gerektiğini nazikçe söyle.")

        # Son birkaç tur — sohbetin akışı kopmasın
        if isinstance(gecmis, list) and gecmis:
            satir = []
            for t in gecmis[-6:]:
                if not isinstance(t, dict):
                    continue
                kim = "Soru" if t.get("kim") == "kul" else "Üstat"
                satir.append(f"{kim}: {str(t.get('metin', ''))[:300]}")
            if satir:
                parcalar.append("=== ÖNCEKİ KONUŞMA ===\n" + "\n".join(satir))

        parcalar.append("=== ŞİMDİKİ SORU ===\n" + soru)

        sonuc = gemini_json(parcalar, SOR_TOOL["input_schema"], 400,
                            uc="sor", model=MODEL_UCUZ, sicaklik=0.7)
        if not sonuc:
            return jsonify({"error": "bos", "mesaj": "Üstat cevap vermedi, tekrar sor."}), 502
        return jsonify(sonuc)

    except ValueError as e:
        if "yarida_kesildi" in str(e):
            return jsonify({"error": "kesildi", "mesaj": "Üstat sözünü tamamlayamadı. Tekrar sor."}), 502
        return jsonify({"error": str(e)}), 500
    except anthropic.APIError as e:
        return jsonify({"error": "api", "mesaj": f"Bağlantı hatası: {str(e)[:120]}"}), 502
    except Exception as e:
        return jsonify({"error": "genel", "mesaj": f"Beklenmeyen hata: {str(e)[:120]}"}), 500


@app.route("/ses", methods=["POST"])
@korumali("ses", agir=False)
def ses():
    if not ELEVEN_API_KEY:
        return jsonify({"error": "ses_kurulmadi",
                        "mesaj": "Ses için ELEVENLABS_API_KEY tanımlı değil."}), 501
    if not ELEVEN_VOICE_ID:
        return jsonify({"error": "ses_kimligi_yok",
                        "mesaj": "Ses için ELEVENLABS_VOICE_ID tanımlı değil."}), 501
    try:
        data = request.get_json()
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Metin boş."}), 400
        text = text[:700]  # kotayı korumak için makul bir üst sınır

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
        body = json.dumps({
            "text": text,
            "model_id": "eleven_multilingual_v2",
            # Kalın ve tok bir üstat sesi için:
            #  stability yüksek  -> titremeyen, sakin, vakur okuma
            #  style düşük       -> abartısız, tiyatral olmayan
            #  speaker_boost     -> gövdeyi ve alt frekansları öne çıkarır
            #  speed < 1         -> ağır, ölçülü konuşma
            "voice_settings": {
                "stability": float(os.environ.get("SES_STABILITY", "0.72")),
                "similarity_boost": float(os.environ.get("SES_BENZERLIK", "0.85")),
                "style": float(os.environ.get("SES_STIL", "0.15")),
                "speed": float(os.environ.get("SES_HIZ", "0.92")),
                "use_speaker_boost": True,
            },
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "xi-api-key": ELEVEN_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio = resp.read()
        return Response(audio, mimetype="audio/mpeg")

    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        import logging
        logging.error("ElevenLabs %s: %s", e.code, detail[:500])
        aciklama = {
            401: "Ses anahtarı geçersiz ya da kota doldu.",
            402: ("Bu ses ücretsiz hesapla API'den kullanılamıyor. "
                  "Hazır (premade) bir ses seç ya da Starter planına geç."),
            403: "Ses anahtarının izni yok. Text to Speech iznini aç.",
            404: "Ses kimliği bulunamadı. Voice ID'yi kontrol et.",
            422: "Ses ayarları kabul edilmedi.",
            429: "Çok sık istek gitti. Biraz bekle.",
        }.get(e.code, f"Ses servisi hata verdi ({e.code}).")
        return jsonify({"error": "ses", "mesaj": aciklama}), 502
    except Exception as e:
        return jsonify({"error": f"Beklenmeyen hata: {str(e)}"}), 500


@app.route("/_sesler")
def _sesler():
    """Bu anahtarın API'den KULLANABİLDİĞİ sesleri listeler.

    Kütüphaneden ses seçip 402 yemek yerine, doğrudan hesabına tanımlı
    sesleri görürsün. Listede çıkan her ses çalışır.
    Kullanım: /_sesler?k=<DURUM_ANAHTARI>
    """
    anahtar = os.environ.get("DURUM_ANAHTARI")
    if not anahtar or request.args.get("k") != anahtar:
        return "", 404
    if not ELEVEN_API_KEY:
        return jsonify({"hata": "ELEVENLABS_API_KEY tanimli degil"}), 501
    try:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": ELEVEN_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            ham = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return jsonify({"hata": f"ElevenLabs {e.code}",
                        "detay": e.read().decode("utf-8", errors="ignore")[:300]}), 502
    except Exception as e:
        return jsonify({"hata": str(e)[:200]}), 500

    liste = []
    for v in ham.get("voices", []):
        etiket = v.get("labels") or {}
        tur = v.get("category")
        liste.append({
            "ad": v.get("name"),
            "voice_id": v.get("voice_id"),
            "tur": tur,
            "ucretsizde_calisir": tur == "premade",
            "cinsiyet": etiket.get("gender"),
            "yas": etiket.get("age"),
            "tanim": etiket.get("description"),
            "aksan": etiket.get("accent"),
        })
    # Erkek ve derin/olgun olanları öne al — üstat karakterine en yakınlar
    def puan(v):
        p = 0
        if v.get("ucretsizde_calisir"):
            p -= 20                      # ücretsizde çalışanlar en başa
        if (v.get("cinsiyet") or "").lower() == "male":
            p -= 4
        for k in ("deep", "calm", "mature", "narration", "old", "middle"):
            if k in ((v.get("tanim") or "") + " " + (v.get("yas") or "")).lower():
                p -= 2
        return p
    liste.sort(key=puan)
    return jsonify({
        "kullanilabilir_ses_sayisi": len(liste),
        "not": ("UCRETSIZ planda YALNIZCA 'ucretsizde_calisir: true' olanlar calisir. "
                "Digerleri kutuphane sesidir ve 402 verir."),
        "ucretsizde_calisan_sayisi": sum(1 for v in liste if v["ucretsizde_calisir"]),
        "sesler": liste,
    })


@app.route("/_durum")
def _durum():
    """Harcama panosu. Render'da DURUM_ANAHTARI ayarla, sonra
    https://<site>/_durum?k=<anahtar> adresinden bak."""
    anahtar = os.environ.get("DURUM_ANAHTARI")
    if not anahtar or request.args.get("k") != anahtar:
        return "", 404
    d = guard_durum(); d["surum"] = SURUM
    return jsonify(d)


if __name__ == "__main__":
    # Bulutta (Render vb.) PORT ortam değişkeni gelir; lokalde 5000 kullanılır.
    port = int(os.environ.get("PORT", 5000))
    print("\n  İlm-i Sîmâ sunucusu başlatılıyor...")
    print(f"  Lokal kullanım:  http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)

