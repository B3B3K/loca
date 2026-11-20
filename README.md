# Lokal Ai Locası

Python toolbox + yerel LLM (Ollama) konu spesifik agentic kütüphane ve promptlar.

## Kullanım (CLI)
    ollama run qwen3.5:latest
    #konu spesifik başlatıcılar için klasörleri inceleyin


## Özelleştirme
- Promptlar: `apkagent/prompts/` (`system.md`, `tools.md`, `fewshot.json`, `phases/*.md`, `workflow.json`).
  `--prompts-dir` içine aynı adlı dosya koymak yeterli; `@@DEĞİŞKEN@@` yer tutucuları kullanılır.
- Faz ekle/çıkar: `workflow.json`.
- Yeni araç: `@registry.register()` ile fonksiyon ekle, `tools.md`'ye tarif yaz.
- İlerleme: `on_event(kind, data)` callback'i (token, step, call, observation, note, warn, phase).
- Notlar `out/sdk/.notes.json` içinde saklanır, yeniden başlatmada devam eder.

## Data Miner Test Dosyaları
- Şuanda QWEN2.5VL Model kullanılıyor (llama-server üstünden)
- OCR'ye geçilme planı var
- 17 saatte 6 Dosya işledi
- 26 MB kaynak dosya
- 180 MB kaynak dosyadan üretilmiş SPLIT IMAGES
- Model VRAM'de olmadığı için CPU Offload aktif
- performans çok zayıf
