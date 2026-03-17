<div align="center">
  <a href="https://github.com/ViminioSM/MedStitch">
    <img alt="MedStitch Logo" width="180" src="https://github.com/ViminioSM/MedStitch/raw/dev/assets/SmartStitchLogo.png">
  </a>

  <h1>MedStitch</h1>
  <p><strong>Stitch + Slice para webtoon/manhwa/manhua</strong><br/>Rápido, estável e pronto para fluxo de edição.</p>

  <p>
    <a href="https://github.com/ViminioSM/MedStitch/releases/latest"><img src="https://img.shields.io/github/v/release/ViminioSM/MedStitch?label=release" alt="Latest Release"></a>
    <a href="https://github.com/ViminioSM/MedStitch/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/ViminioSM/MedStitch/ci.yml?label=ci" alt="CI"></a>
    <a href="https://github.com/ViminioSM/MedStitch/actions/workflows/build.yml"><img src="https://img.shields.io/github/actions/workflow/status/ViminioSM/MedStitch/build.yml?label=release" alt="Release Workflow"></a>
    <a href="https://github.com/ViminioSM/MedStitch/releases"><img src="https://img.shields.io/github/downloads/ViminioSM/MedStitch/total" alt="Downloads"></a>
    <a href="https://github.com/ViminioSM/MedStitch/blob/dev/LICENSE"><img src="https://img.shields.io/github/license/ViminioSM/MedStitch" alt="License"></a>
  </p>
</div>

---

## O que o MedStitch faz

MedStitch junta múltiplas imagens em páginas longas e depois corta em painéis de leitura.

Objetivos do projeto:
- Preservar qualidade visual.
- Evitar cortes ruins em texto e arte.
- Manter fluxo simples para produção.
- Atender GUI e CLI.

## Destaques

### Interface GUI
- Stitch/slice por pasta.
- Detectores:
  - Pixel comparison (smart).
  - Direct slicing.
- Formatos de saída: .png, .jpg, .webp, .bmp, .psd, .tiff, .tga.
- Enforce de largura: none, automático, customizado.
- Perfis e persistência de configurações.
- Pós-processamento com placeholders [stitched] e [processed].
- Integração opcional com ComicZip.
- Menu de contexto no Windows.
- Checagem de update e auto-update para app compilado.

### Sistema de Watermark
- Fullpage watermark em blocos uniformes.
- Overlay watermark com posição/opacidade/escala.
- Inserção de header e footer.
- Toggle rápido via menu de contexto.

### Console (CLI)
- Pipeline para batch/headless.
- Opções principais de detector/corte via argumentos.

---

## Começando rápido

### Windows (release)
1. Baixe a versão mais recente em Releases.
2. Extraia o pacote.
3. Execute SmartStitch.exe.
4. Escolha a pasta de entrada.
5. Ajuste detector/saída.
6. Inicie o processamento.

### Rodando via código-fonte
1. Instale Python 3.11+.
2. Instale dependências.

```bash
pip install -r requirements.txt
```

3. Rode GUI.

```bash
python SmartStitchGUI.py
```

4. Ou rode CLI.

```bash
python SmartStitchConsole.py -i "./chapter" -sh 7500 -t .png
```

---

## CLI (resumo)

```text
python SmartStitchConsole.py [-h] -i INPUT_FOLDER -sh SPLIT_HEIGHT
                             [-t {.png,.jpg,.webp,.bmp,.psd,.tiff,.tga}]
                             [-cw CUSTOM_WIDTH]
                             [-dt {none,pixel}]
                             [-s [0-100]]
                             [-lq [1-100]]
                             [-ip IGNORABLE_PIXELS]
                             [-sl [1-100]]
```

---

## Build local

```bash
python -m scripts.build
```

Saída esperada:
- dist/SmartStitch/SmartStitch.exe

---

## Atualização automática no app

Endpoint usado:
- https://api.github.com/repos/ViminioSM/MedStitch/releases/latest

Comportamento:
- Compara versão local com tag da release.
- Se houver versão mais nova, o app compilado pode baixar ZIP, aplicar update e reiniciar.

Requisitos:
- Tags no formato vX.Y.Z.
- Release com asset .zip.

---

## Pipeline GitHub Actions

Workflows:
- .github/workflows/ci.yml
- .github/workflows/auto-tag.yml
- .github/workflows/build.yml

Fluxo:
1. Push em dev/main dispara CI (build de validação).
2. Se o título do commit tiver versão semântica, auto-tag cria vX.Y.Z.
3. Auto-tag dispara workflow de release.
4. Release workflow compila + publica a release com ZIP.

Nome do asset:
- MedStitch-vX.Y.Z-windows.zip

Exemplo de deploy:

```bash
git commit -m "3.2.0"
git push origin main
```

---

## Estrutura do projeto

- gui/: interface, controller e orquestração.
- console/: launcher e fluxo CLI.
- core/detectors/: detectores de corte.
- core/services/: image IO, manipulação, watermark, postprocess, settings.
- core/models/: modelos de configuração e work directory.
- scripts/: build e utilitários.

---

## Troubleshooting

- Menu de contexto duplicado:
  - Remova pelo app.
  - Instale novamente.
- Update não encontrado:
  - Verifique internet/acesso ao GitHub.
  - Confirme tag válida e ZIP na release.
- Pós-processamento falhando:
  - Verifique caminho do executável e argumentos.

---

## Reportando problemas

Ao abrir issue, inclua:
- Passos executados.
- Comportamento esperado vs atual.
- Logs da pasta __logs__.
- Comando ou configuração usada.

---

## Licença

Projeto distribuído sob os termos do arquivo LICENSE.
