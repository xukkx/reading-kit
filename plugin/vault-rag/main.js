/* Vault RAG — ask-while-reading MVP.
 * Pure JS, mobile-compatible (no Node APIs; requestUrl for HTTP).
 * Backend: scripts/rag.py serve  (POST {q, provider?, k?} -> {answer, sources}) */
'use strict';

const { Plugin, ItemView, PluginSettingTab, Setting, requestUrl, Notice, MarkdownRenderer, Modal, FuzzySuggestModal, MarkdownView, Menu, Platform } = require('obsidian');

/* 跳转页码: fuzzy picker over all page markers in the active note */
class PageJumpModal extends FuzzySuggestModal {
  constructor(app, pages, onPick) {
    super(app);
    this.pages = pages; this.onPick = onPick;
    this.setPlaceholder('输入页码，如 55 …');
  }
  getItems() { return this.pages; }
  getItemText(p) { return `p.${p.page}　${p.snippet}`; }
  onChooseItem(p) { this.onPick(p); }
}

/* 划线批注 modal: quote → optional comment → saved to <folder>/批注.md, source gets ==marked== */
class AnnotateModal extends Modal {
  constructor(app, plugin, quote, file) {
    super(app);
    this.plugin = plugin; this.quote = quote; this.file = file;
  }
  onOpen() {
    const c = this.contentEl;
    c.createEl('div', { cls: 'vrag-modal-quote', text: `「${this.quote.slice(0, 200)}」` });
    this.ta = c.createEl('textarea', { cls: 'vrag-modal-ta', attr: { placeholder: '💭 写点想法…（可留空，只划线）', rows: 4 } });
    const row = c.createDiv({ cls: 'vrag-modal-row' });
    const save = row.createEl('button', { text: '保存批注', cls: 'mod-cta' });
    save.addEventListener('click', () => this.save());
    this.ta.focus();
  }
  async save() {
    const { vault } = this.app;
    const content = await vault.read(this.file);
    const idx = content.indexOf(this.quote);
    let page = '';
    if (idx >= 0) {
      const marks = [...content.slice(0, idx).matchAll(/data-p="(\d+)"|%%p\.(\d+)%%/g)];
      if (marks.length) { const m = marks[marks.length - 1]; page = m[1] || m[2]; }
      // persist the highlight in the source (only if the match is unique & unmarked)
      if (content.indexOf(this.quote, idx + 1) === -1 && !content.slice(idx - 2, idx).includes('==')) {
        await vault.modify(this.file,
          content.slice(0, idx) + '==' + this.quote + '==' + content.slice(idx + this.quote.length));
      }
    }
    const comment = this.ta.value.trim();
    const dir = this.file.parent && this.file.parent.path !== '/' ? this.file.parent.path : '';
    const notePath = (dir ? dir + '/' : '') + '批注.md';
    const entry = `\n> "${this.quote}"${page ? ` (p.${page})` : ''}\n` + (comment ? `\n💭 ${comment}\n` : '');
    const existing = vault.getAbstractFileByPath(notePath);
    if (existing) await vault.append(existing, entry);
    else await vault.create(notePath,
      `---\ntags: [annotations]\n---\n\n# ${this.file.parent.name} · 批注\n\n来源：[[${this.file.basename}]]\n${entry}`);
    new Notice(`已保存批注${page ? ` (p.${page})` : ''}`);
    this.close();
  }
}

const VIEW_TYPE = 'vault-rag-view';

const DEFAULTS = {
  // Comma-separated list — settings sync with the vault, so listing BOTH the
  // PC-local and the LAN address makes the same config work on every device.
  endpoint: 'http://localhost:8766',
  provider: '',
  topK: 6,
  // Desktop only: command run once per session when no endpoint answers /health,
  // e.g.  wscript.exe "D:\MyProject\start_rag_server.vbs"
  autostartCmd: '',
};

class RagView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
  }
  getViewType() { return VIEW_TYPE; }
  getDisplayText() { return '知识库问答'; }
  getIcon() { return 'messages-square'; }

  async onOpen() {
    const root = this.contentEl;
    root.empty();
    root.addClass('vrag');

    this.log = root.createDiv({ cls: 'vrag-log' });
    const hello = this.log.createDiv({ cls: 'vrag-hello' });
    hello.createDiv({ text: '📖 读书时：选中文字 → 弹出小工具条 → ✍️批注 或 ❓问AI' });
    hello.createDiv({ text: '💬 或在下面直接提问，回答只依据你的笔记、带来源。' });
    const quick = hello.createDiv({ cls: 'vrag-quick' });
    const qb = (txt, fn) => { const b = quick.createEl('button', { text: txt }); b.addEventListener('click', fn); };
    qb('🔢 跳转页码', () => this.plugin.jumpToPage());
    qb('🔄 重建索引', () => this.plugin.app.commands.executeCommandById('vault-rag:rebuild-index'));
    qb('📘 使用说明', () => this.app.workspace.openLinkText('使用说明', '', false));

    const bar = root.createDiv({ cls: 'vrag-bar' });
    this.input = bar.createEl('textarea', { cls: 'vrag-input', attr: { placeholder: '问点什么…（回车发送，Shift+回车换行）', rows: 2 } });
    const btn = bar.createEl('button', { cls: 'vrag-send', text: '问' });

    const send = () => {
      const q = this.input.value.trim();
      if (q) { this.input.value = ''; this.ask(q); }
    };
    btn.addEventListener('click', send);
    this.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
  }

  async ask(q) {
    const s = this.plugin.settings;
    this.log.createDiv({ cls: 'vrag-q', text: q });
    const a = this.log.createDiv({ cls: 'vrag-a' });
    a.setText('检索中…');
    this.log.scrollTo({ top: this.log.scrollHeight });
    try {
      const body = { q, k: s.topK };
      if (s.provider) body.provider = s.provider;
      const base = await this.plugin.resolveEndpoint();
      const r = await requestUrl({
        url: base,
        method: 'POST',
        contentType: 'application/json',
        body: JSON.stringify(body),
        throw: false,
      });
      if (r.status !== 200) { this.plugin._goodEp = null; throw new Error((r.json && r.json.error) || `HTTP ${r.status}`); }
      const { answer, sources, provider } = r.json;
      a.empty();
      await MarkdownRenderer.render(this.app, answer, a.createDiv(), '', this.plugin);
      const meta = a.createDiv({ cls: 'vrag-meta' });
      const save = meta.createSpan({ cls: 'vrag-src vrag-save', text: '💾 存入笔记' });
      save.addEventListener('click', async () => {
        const path = '00-Inbox/AI问答存档.md';
        const src = (sources || []).map((h, i) =>
          `[${i + 1}] [[${h.file.replace(/\\/g, '/').replace(/\.md$/, '')}]]${h.page ? ' p.' + h.page : ''}`).join(' · ');
        const entry = `\n## ${q}\n\n${answer}\n\n来源：${src}\n\n*（${provider} · ${new Date().toISOString().slice(0, 10)}）*\n\n---\n`;
        const f = this.app.vault.getAbstractFileByPath(path);
        if (f) await this.app.vault.append(f, entry);
        else await this.app.vault.create(path, `---\ntags: [ai-qa]\n---\n\n# AI 问答存档\n${entry}`);
        save.setText('✅ 已存');
        new Notice('已存入 00-Inbox/AI问答存档');
      });
      meta.createSpan({ text: `来源（${provider}）：` });
      (sources || []).forEach((h, i) => {
        const chip = meta.createSpan({ cls: 'vrag-src', text: `[${i + 1}] ${h.file.split(/[\\/]/).pop().replace(/\.md$/, '')}${h.page ? ' p.' + h.page : ''}` });
        chip.addEventListener('click', async () => {
          const path = h.file.replace(/\\/g, '/');
          const f = this.app.vault.getAbstractFileByPath(path);
          if (!f) { this.app.workspace.openLinkText(path.replace(/\.md$/, ''), '', false); return; }
          const leaf = this.app.workspace.getLeaf(false);
          await leaf.openFile(f);
          if (h.page) {
            // page markers are <span class="pg" data-p="N"> (legacy: %%p.N%%)
            const text = await this.app.vault.cachedRead(f);
            const i = text.split('\n').findIndex(l =>
              l.includes(`data-p="${h.page}"`) || l.includes(`%%p.${h.page}%%`));
            if (i >= 0) setTimeout(() => leaf.view.setEphemeralState({ scroll: i }), 120);
          }
        });
      });
    } catch (e) {
      a.setText(`⚠️ ${e.message}`);
    }
    this.log.scrollTo({ top: this.log.scrollHeight });
  }
}

class RagSettingTab extends PluginSettingTab {
  constructor(app, plugin) { super(app, plugin); this.plugin = plugin; }
  display() {
    const c = this.containerEl;
    c.empty();
    new Setting(c).setName('RAG 服务地址（可填多个，逗号分隔）')
      .setDesc('插件按顺序尝试，自动选可用的。推荐同时填电脑本机和局域网地址，例如：http://localhost:8766, http://192.168.1.5:8766 —— 这样同一份设置在电脑和手机上都能用（设置随 vault 同步）')
      .addText(t => t.setValue(this.plugin.settings.endpoint)
        .onChange(async v => { this.plugin.settings.endpoint = v.trim(); this.plugin._goodEp = null; await this.plugin.saveSettings(); }));
    new Setting(c).setName('测试连接')
      .setDesc('逐个探测上面的地址')
      .addButton(b => b.setButtonText('测试').setCta().onClick(async () => {
        b.setButtonText('测试中…');
        try {
          const ep = await this.plugin.resolveEndpoint(true);
          const r = await requestUrl({ url: ep + '/health', method: 'GET' });
          new Notice(`✅ 已连接 ${ep} — 知识库 ${r.json.chunks} 条`);
        } catch (e) { new Notice('⚠️ ' + e.message, 10000); }
        b.setButtonText('测试');
      }));
    new Setting(c).setName('自动启动命令（仅电脑端）')
      .setDesc('连接失败时自动运行的命令，用来拉起 RAG 服务。推荐指向项目里的启动脚本，例如：wscript.exe "D:\\MyProject\\start_rag_server.vbs"。留空则不自动启动；手机端忽略此项。')
      .addText(t => t.setValue(this.plugin.settings.autostartCmd)
        .onChange(async v => { this.plugin.settings.autostartCmd = v.trim(); await this.plugin.saveSettings(); }));
    new Setting(c).setName('回答模型 provider')
      .setDesc('留空用服务端默认；可填 deepseek / mimo / ollama-cloud（需在 .rag/providers.json 配置）')
      .addText(t => t.setValue(this.plugin.settings.provider)
        .onChange(async v => { this.plugin.settings.provider = v.trim(); await this.plugin.saveSettings(); }));
    new Setting(c).setName('检索条数 k')
      .addSlider(sl => sl.setLimits(3, 12, 1).setValue(this.plugin.settings.topK).setDynamicTooltip()
        .onChange(async v => { this.plugin.settings.topK = v; await this.plugin.saveSettings(); }));
  }
}

module.exports = class RagPlugin extends Plugin {
  async onload() {
    await this.loadSettings();
    this.registerView(VIEW_TYPE, (leaf) => new RagView(leaf, this));

    // ONE hub button — every reading tool hangs off this menu.
    this.addRibbonIcon('book-open', '📖 阅读工具（全部功能在这里）', (evt) => {
      const menu = new Menu();
      menu.addItem(i => i.setTitle('❓ 知识库问答面板').setIcon('messages-square')
        .onClick(() => this.activateView()));
      menu.addItem(i => i.setTitle('✍️ 划线批注（先选中文字）').setIcon('highlighter')
        .onClick(() => {
          const sel = (activeWindow.getSelection() || '').toString().trim();
          const f = this.app.workspace.getActiveFile();
          if (!sel) { new Notice('先选中一段文字，也可以直接选中后用弹出的小工具条'); return; }
          if (f) new AnnotateModal(this.app, this, sel, f).open();
        }));
      menu.addItem(i => i.setTitle('🔢 跳转页码').setIcon('hash')
        .onClick(() => this.jumpToPage()));
      menu.addSeparator();
      menu.addItem(i => i.setTitle('🔄 重建知识库索引（批注入库后跑一次）').setIcon('refresh-cw')
        .onClick(() => this.app.commands.executeCommandById('vault-rag:rebuild-index')));
      menu.addItem(i => i.setTitle('📘 使用说明').setIcon('help-circle')
        .onClick(() => this.app.workspace.openLinkText('使用说明', '', false)));
      menu.showAtMouseEvent(evt);
    });

    this.addCommand({
      id: 'ask-about-selection',
      name: '问知识库：解释选中的文字',
      callback: async () => {
        const sel = (activeWindow.getSelection() || '').toString().trim();
        if (!sel) { new Notice('先选中一段文字'); return; }
        const view = await this.activateView();
        if (view) view.ask(`请结合知识库解释这段话的含义与相关背景：「${sel.slice(0, 500)}」`);
      },
    });
    this.addCommand({
      id: 'open-panel',
      name: '打开知识库问答面板',
      callback: () => this.activateView(),
    });
    this.addCommand({
      id: 'jump-to-page',
      name: '跳转页码（当前书）',
      callback: () => this.jumpToPage(),
    });
    this.addRibbonIcon('hash', '跳转页码', () => this.jumpToPage());
    this.addCommand({
      id: 'annotate-selection',
      name: '划线批注：保存选中的文字',
      callback: () => {
        const sel = (activeWindow.getSelection() || '').toString().trim();
        const file = this.app.workspace.getActiveFile();
        if (!sel || !file) { new Notice('先在笔记里选中要划线的文字'); return; }
        new AnnotateModal(this.app, this, sel, file).open();
      },
    });
    this.addCommand({
      id: 'rebuild-index',
      name: '重建知识库索引（把新批注/笔记纳入问答）',
      callback: async () => {
        new Notice('重建索引中…');
        try {
          const base = await this.resolveEndpoint();
          const r = await requestUrl({
            url: base, method: 'POST',
            contentType: 'application/json', body: JSON.stringify({ cmd: 'rebuild' }), throw: false,
          });
          if (r.status !== 200) { this._goodEp = null; throw new Error((r.json && r.json.error) || 'HTTP ' + r.status); }
          new Notice(`✅ 索引已更新：${r.json.chunks} 条`);
        } catch (e) { new Notice('⚠️ ' + e.message, 8000); }
      },
    });

    // right-click menu on selection (editing mode)
    this.registerEvent(this.app.workspace.on('editor-menu', (menu, editor) => {
      const s = editor.getSelection().trim();
      if (!s) return;
      menu.addItem(i => i.setTitle('✍️ 划线批注').setIcon('highlighter').onClick(() => {
        const f = this.app.workspace.getActiveFile();
        if (f) new AnnotateModal(this.app, this, s, f).open();
      }));
      menu.addItem(i => i.setTitle('❓ 问知识库').setIcon('messages-square').onClick(async () => {
        const v = await this.activateView();
        if (v) v.ask(`请结合知识库解释这段话：「${s.slice(0, 500)}」`);
      }));
    }));

    this.setupSelectionToolbar();
    this.addSettingTab(new RagSettingTab(this.app, this));
  }

  /* floating mini-toolbar near any text selection (works in reading mode too) */
  setupSelectionToolbar() {
    this.selBar = document.body.createDiv({ cls: 'vrag-selbar' });
    this.selBar.style.display = 'none';
    const mk = (txt, fn) => {
      const b = this.selBar.createEl('button', { text: txt });
      b.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); fn(); this.hideSelBar(); });
    };
    mk('✍️ 批注', () => {
      const f = this.app.workspace.getActiveFile();
      if (this.lastSel && f) new AnnotateModal(this.app, this, this.lastSel, f).open();
    });
    mk('❓ 问AI', async () => {
      if (!this.lastSel) return;
      const v = await this.activateView();
      if (v) v.ask(`请结合知识库解释这段话：「${this.lastSel.slice(0, 500)}」`);
    });
    this.registerDomEvent(document, 'selectionchange', () => {
      clearTimeout(this._selT);
      this._selT = setTimeout(() => this.maybeShowSelBar(), 300);
    });
    this.registerDomEvent(document, 'mousedown', (e) => {
      if (!this.selBar.contains(e.target)) this.hideSelBar();
    });
    this.registerDomEvent(document, 'touchstart', (e) => {
      if (!this.selBar.contains(e.target)) this.hideSelBar();
    });
    // touch: tap the toolbar buttons via touchend (mousedown may not fire)
    this.selBar.querySelectorAll('button').forEach(b => {
      b.addEventListener('touchend', (e) => { e.preventDefault(); e.stopPropagation(); b.dispatchEvent(new MouseEvent('mousedown')); });
    });
  }

  maybeShowSelBar() {
    const sel = activeWindow.getSelection();
    const txt = sel ? sel.toString().trim() : '';
    const host = sel && sel.anchorNode &&
      (sel.anchorNode.parentElement || sel.anchorNode).closest?.('.markdown-preview-view, .cm-content');
    if (!txt || txt.length < 2 || !host) { this.hideSelBar(); return; }
    this.lastSel = txt;
    try {
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      this.selBar.style.display = 'flex';
      this.selBar.style.left = Math.max(8, Math.min(window.innerWidth - 170, rect.left + rect.width / 2 - 78)) + 'px';
      // mobile: native selection menu sits above the selection — go below it
      this.selBar.style.top = (Platform.isMobile
        ? Math.min(window.innerHeight - 60, rect.bottom + 14)
        : Math.max(8, rect.top - 46)) + 'px';
    } catch (e) { this.hideSelBar(); }
  }

  hideSelBar() { if (this.selBar) this.selBar.style.display = 'none'; }

  /* Try each configured endpoint (comma-separated) until one answers /health.
     Cached per session; cache cleared on request failure. */
  async resolveEndpoint(force) {
    if (this._goodEp && !force) return this._goodEp;
    const eps = (this.settings.endpoint || '').split(/[,;\s，；]+/).map(e => e.trim().replace(/\/$/, '')).filter(Boolean);
    const probe = async () => {
      for (const ep of eps) {
        try {
          const r = await requestUrl({ url: ep + '/health', method: 'GET', throw: false });
          if (r.status === 200) { this._goodEp = ep; return ep; }
        } catch (e) { /* try next */ }
      }
      return null;
    };
    let ep = await probe();
    if (!ep && await this.tryAutostart()) {
      // give the server a moment to come up, then re-probe a few times
      for (let i = 0; i < 8 && !ep; i++) {
        await new Promise(res => setTimeout(res, 1000));
        ep = await probe();
      }
      if (ep) new Notice('✅ RAG 服务已自动启动');
    }
    if (ep) return ep;
    throw new Error(`无法连接 RAG 服务（已尝试：${eps.join('、') || '（未配置地址）'}）。` +
      `确认电脑上运行着 python scripts\\rag.py serve；手机需在设置里加上电脑的局域网地址。`);
  }

  /* Desktop only: launch the configured server command, once per session.
     Node APIs are unavailable on mobile, so everything stays behind guards —
     on phones this quietly does nothing and the plugin remains mobile-safe. */
  async tryAutostart() {
    const cmd = (this.settings.autostartCmd || '').trim();
    if (!cmd || Platform.isMobile || this._autostarted) return false;
    this._autostarted = true;   // one attempt per session; no spawn loops
    try {
      new Notice('RAG 服务未运行，正在自动启动…');
      require('child_process').exec(cmd, { windowsHide: true });
      return true;
    } catch (e) {
      new Notice('⚠️ 自动启动失败：' + e.message, 8000);
      return false;
    }
  }

  async jumpToPage() {
    const { workspace } = this.app;
    // Ribbon clicks steal focus to the sidebar — the "active view" may not be
    // the book pane. Fall back to the most recent markdown leaf in the main area.
    let view = workspace.getActiveViewOfType(MarkdownView);
    if (!view) {
      const leaf = workspace.getMostRecentLeaf(workspace.rootSplit);
      if (leaf && leaf.view instanceof MarkdownView) view = leaf.view;
    }
    if (!view) {
      const md = workspace.getLeavesOfType('markdown');
      if (md.length) view = md[0].view;
    }
    const file = view && view.file;
    if (!file) { new Notice('先打开一本书（Books 文件夹里的正文）'); return; }
    const text = await this.app.vault.cachedRead(file);
    const lines = text.split('\n');
    const pages = [];
    lines.forEach((l, i) => {
      for (const m of l.matchAll(/<span class="pg" data-p="(\d+)"><\/span>|%%p\.(\d+)%%/g)) {
        const after = l.slice(m.index + m[0].length).replace(/<[^>]+>|==|%%[^%]*%%/g, '');
        pages.push({ page: m[1] || m[2], line: i, snippet: after.slice(0, 24) || lines[i + 1]?.slice(0, 24) || '' });
      }
    });
    if (!pages.length) { new Notice('这篇笔记里没有页码标记'); return; }
    new PageJumpModal(this.app, pages, (p) => {
      view.setEphemeralState({ scroll: p.line });
      new Notice(`→ p.${p.page}`);
    }).open();
  }

  async activateView() {
    const { workspace } = this.app;
    let leaf = workspace.getLeavesOfType(VIEW_TYPE)[0];
    if (!leaf) {
      leaf = workspace.getRightLeaf(false);
      await leaf.setViewState({ type: VIEW_TYPE, active: true });
    }
    workspace.revealLeaf(leaf);
    return leaf.view instanceof RagView ? leaf.view : null;
  }

  async loadSettings() { this.settings = Object.assign({}, DEFAULTS, await this.loadData()); }
  async saveSettings() { await this.saveData(this.settings); }

  onunload() { if (this.selBar) this.selBar.remove(); }
};
