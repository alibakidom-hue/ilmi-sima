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

app = Flask(__name__, static_folder=".", static_url_path="")

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
- Sadece geçerli JSON döndür, başka HİÇBİR metin yazma (markdown, açıklama, ``` yok).
- Yorumlar gerçekten gördüğün yüz hatlarına dayansın, genel geçer olmasın.
- Bu eğlence ve kültürel bir uygulamadır; tıbbi/kesin iddialarda bulunma, klasik üslupta yorumla.

Şu yapıda JSON döndür:
{
  "dominantTrait": "baskın mizaç (Arapça, harekeli, örn: الحِكْمَة)",
  "dominantTraitTR": "Türkçe karşılığı",
  "traits": [
    {"name": "Alın", "arabic": "الجَبْهَة", "description": "gözleme dayalı 2-3 cümle yorum", "intensity": 0-100 arası sayı},
    {"name": "Gözler", "arabic": "العُيُون", "description": "...", "intensity": sayı},
    {"name": "Burun", "arabic": "الأَنْف", "description": "...", "intensity": sayı},
    {"name": "Ağız ve Dudaklar", "arabic": "الشِّفَاه", "description": "...", "intensity": sayı},
    {"name": "Yüz Hattı", "arabic": "الوَجْه", "description": "...", "intensity": sayı}
  ],
  "overall": "bütünsel kıraat, 4-5 cümle, klasik Osmanlı üslubu ama anlaşılır modern Türkçe"
}"""


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
            max_tokens=1500,
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

        # Yanıttan metni topla
        text = "".join(
            block.text for block in message.content if block.type == "text"
        )

        # JSON bloğunu güvenli biçimde ayıkla
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return jsonify({"error": "Model JSON döndürmedi", "raw": text[:300]}), 502

        parsed = json.loads(text[start : end + 1])
        return jsonify(parsed)

    except anthropic.APIError as e:
        return jsonify({"error": f"API hatası: {str(e)}"}), 502
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON ayrıştırma hatası: {str(e)}"}), 502
    except Exception as e:
        return jsonify({"error": f"Beklenmeyen hata: {str(e)}"}), 500


if __name__ == "__main__":
    # Bulutta (Render vb.) PORT ortam değişkeni gelir; lokalde 5000 kullanılır.
    port = int(os.environ.get("PORT", 5000))
    print("\n  İlm-i Sîmâ sunucusu başlatılıyor...")
    print(f"  Lokal kullanım:  http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
