# secagent: prod öncesi yerel güvenlik analizi

Aynı `apkagent` çekirdeği (Agent/LLM/PromptStore/Workflow) üzerine kurulu. Kod makineden çıkmaz
(LLM yerel; `--online` vermezsen ağ erişimi yok).

## Kullanım
    python -m secagent --project ./myapp --backend ollama --model qwen2.5-coder:14b --out security_out
    python -m secagent --project ./myapp --model ... --fail-on high     # CI kapısı: high+ varsa exit 2
    python -m secagent --project ./myapp --model ... --only recon automated   # sadece bazı fazlar
    python -m secagent --project ./myapp --model ... --online           # OSV.dev CVE sorgusu (sadece paket adı/sürüm gider)

Çıktılar (`--out`): `REPORT.md`, `findings.json`, `findings.sarif` (GitHub/GitLab code scanning), `candidates.json`, `reports/<faz>.md`.

## Akış
1. recon: stack, entry point, auth modeli, trust boundary
2. automated: `sast_scan` (116 kural), `secrets_scan`, `dep_audit`, kuruluysa semgrep/bandit/gitleaks/trivy → hepsi **aday kuyruğuna** düşer
3. triage x2: `next_candidates` ile 5'erli çek, `report_finding` veya `dismiss`
4. hunt: tarayıcıların kaçırdığı mantık hataları (authz/IDOR, SSRF, upload, session...)
5. infra: Docker/CI/IaC/env
6. summary: risk + BLOCK / CONDITIONAL / OK

## Next.js / React / TypeScript desteği
- Stack otomatik tespit edilir (package.json, tsconfig); `nextjs` ve `frontend` fazları yalnızca ilgili stack varsa çalışır.
- `nextjs_audit` aracı: route handler ve Server Action envanteri (auth/validation var mı), middleware matcher
  analizi, `use client` dosyalarında sunucu env/import sızıntısı, eksik güvenlik başlıkları, `.gitignore`'da `.env`,
  webhook imza ve cron secret kontrolü.
- 40 ek kural (116 toplam): XSS sink'leri, token saklama, open redirect, postMessage, `NEXT_PUBLIC_` ile secret sızıntısı,
  next/image ve config ayarları, Auth.js tuzakları, Prisma/NoSQL enjeksiyon, mass assignment, TypeScript'te doğrulamasız `as T`.
- Stack notları `prompts/stacks/*.md` içinde (system prompt'a otomatik girer, false positive azaltır).
- `rules/advisories.json`: sürüm ipuçları (next, react-server-dom-*, sharp, @sentry/nextjs). 2026-11-13 tarihli
  kamuya açık özetlerden derlendi; otorite değildir, bayatlar. `npm audit`/trivy/`--online` ile doğrula, dosyayı güncel tut.
- Verdict: yüksek/kritik aday triyaj edilmediyse `OK` yerine `INCOMPLETE` döner.

## Güvenilirlik mekanizmaları
- `report_finding` kanıt kodunu dosyada doğrular; eşleşmezse reddeder, ısrar edilirse UNVERIFIED etiketler.
- Raporda secret değerleri maskelenir. Proje dosyalarına hiçbir araç yazmaz.
- Triyaj kuyruğu kapsamı garanti eder (bekleyen aday sayısı raporda görünür).

## Özelleştirme
- Kurallar: `secagent/rules/sast.json`, `secrets.json`; `--rules-dir` içine aynı adlı dosya koy (eklenir).
- Promptlar: `secagent/prompts/` (`--prompts-dir` ile override). Fazlar: `workflow.json`.
- Yeni araç: `registry.register()`; `tools.md`'ye tarif ekle.
