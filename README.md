# apkagent

JADX + yerel LLM (Ollama / llama.cpp) ile APK protokol analizi yapan agentic kütüphane.

## Kullanım (CLI)
    ollama run qwen3.5:latest
    python -m apkagent --apk app.apk --backend ollama --model qwen3.5:latest --lang python --out out
    python -m apkagent --src jadx_out --only recon map          # sadece bazı fazlar
    python -m apkagent --apk app.apk --prompts-dir my_prompts   # promptları override et

## Kullanım (kütüphane)
    from apkagent import LLM, Workflow, build_agent
    llm = LLM("http://127.0.0.1:11434", model="qwen2.5-coder:14b", backend="ollama")
    agent, prompts, vars_ = build_agent("app.apk", "jadx_src", "out/sdk", llm, lang="kotlin")
    Workflow(agent, prompts, vars_).run()
    # veya tek faz: agent.run("PHASE 1 ...", max_steps=10)

## Özelleştirme
- Promptlar: `apkagent/prompts/` (`system.md`, `tools.md`, `fewshot.json`, `phases/*.md`, `workflow.json`).
  `--prompts-dir` içine aynı adlı dosya koymak yeterli; `@@DEĞİŞKEN@@` yer tutucuları kullanılır.
- Faz ekle/çıkar: `workflow.json`.
- Yeni araç: `@registry.register()` ile fonksiyon ekle, `tools.md`'ye tarif yaz.
- İlerleme: `on_event(kind, data)` callback'i (token, step, call, observation, note, warn, phase).
- Notlar `out/sdk/.notes.json` içinde saklanır, yeniden başlatmada devam eder.
