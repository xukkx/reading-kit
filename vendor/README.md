# vendor/ — 可选依赖的"拖入即用"目录

把**不在 PyPI 上的可选 Python 包**的文件夹直接放进这里，脚本会自动找到它，
无需任何 `pip install` 命令。

## 当前支持的可选包

### kb_substrate（原子化知识底座）

把整个 `kb_substrate/` 文件夹（含 `__init__.py`）拖进本目录：

```
<项目根>/
  vendor/
    kb_substrate/
      __init__.py
      ...
```

启用后：

- `import_book.py` 的第 5 阶段会把每一页已校验的 OCR 内容作为可查询的
  "原子"存入 `.rag/kb.sqlite3`（此前的导入可以重跑 `substrate_build.py` 补录）
- `rag.py serve` 的 `/recall`、`/remember` 接口开始工作（交互记忆）

没有它系统一切照常：导入、阅读、划线批注、AI 问答全部不受影响——
缺包时第 5 阶段会打印一条说明并自动跳过。
