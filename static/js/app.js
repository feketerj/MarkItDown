/**
 * MD_CREATOR — Client-side Application Logic
 * Handles file upload, conversion API calls, preview rendering, history, and UI interactions.
 */

(function () {
  'use strict';

  // ── Config ──
  const API_CONVERT = '/api/convert';
  const API_FORMATS = '/api/formats';
  const API_TOKEN = document.querySelector('meta[name="mdcreator-token"]')?.content || '';
  const HISTORY_KEY = 'mdcreator_history';
  const MAX_HISTORY = 20;
  const HISTORY_MARKDOWN_LIMIT = 1024 * 1024;
  const PREVIEW_RENDER_LIMIT = 500 * 1024;

  // ── State ──
  let currentMarkdown = '';
  let currentFilename = '';
  let currentDownloadId = '';
  let currentMarkdownIsPreview = false;
  let selectedEngine = 'standard';

  // ── markdown-it instance ──
  const mdit = window.markdownit({
    html: false,
    linkify: true,
    typographer: true,
    breaks: true,
  });

  // ── DOM refs ──
  const $ = (sel) => document.querySelector(sel);
  const dropZone = $('#drop-zone');
  const fileInput = $('#file-input');
  const browseTrigger = $('#browse-trigger');
  const convertingEl = $('#converting');
  const convertingFilename = $('#converting-filename');
  const resultsEl = $('#results');
  const heroSection = $('#hero-section');
  const rawPane = $('#markdown-raw');
  const previewPane = $('#markdown-preview');
  const editorContainer = $('#editor-container');
  const resultsFilename = $('#results-filename');
  const resultsSize = $('#results-size');
  const resultsTime = $('#results-time');
  const resultsChars = $('#results-chars');
  const lineCount = $('#line-count');
  const toastContainer = $('#toast-container');
  const historyOverlay = $('#history-overlay');
  const historyList = $('#history-list');
  const formatBadges = $('#format-badges');

  // ── Init ──
  function init() {
    loadFormats();
    setupEngineSelector();
    setupDragDrop();
    setupButtons();
    setupTabs();
    setupHistory();
  }

  // ── Engine Selector ──
  function setupEngineSelector() {
    document.querySelectorAll('.engine-option').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.engine-option').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        selectedEngine = btn.dataset.engine;
      });
    });
  }

  // ── Format Badges ──
  async function loadFormats() {
    try {
      const res = await fetch(API_FORMATS);
      const data = await res.json();
      formatBadges.innerHTML = data.formats
        .map((f) => `<span class="format-badge">${f.icon} ${f.label}</span>`)
        .join('');
    } catch {
      formatBadges.innerHTML = '<span class="format-badge">📄 PDF</span><span class="format-badge">📝 Word</span><span class="format-badge">📊 Excel</span><span class="format-badge">🌐 HTML</span>';
    }
  }

  // ── Drag & Drop ──
  function setupDragDrop() {
    ['dragenter', 'dragover'].forEach((evt) =>
      dropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('drag-over');
      })
    );
    ['dragleave', 'drop'].forEach((evt) =>
      dropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('drag-over');
      })
    );
    dropZone.addEventListener('drop', (e) => {
      const file = e.dataTransfer.files[0];
      if (file) convertFile(file);
    });
    dropZone.addEventListener('click', openFilePicker);
    browseTrigger.addEventListener('click', (e) => {
      e.stopPropagation();
      openFilePicker();
    });
    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) convertFile(fileInput.files[0]);
      fileInput.value = '';
    });
  }

  function openFilePicker() {
    if (typeof fileInput.showPicker === 'function') {
      try {
        fileInput.showPicker();
        return;
      } catch {
        // Fall back for browsers that expose showPicker but reject it here.
      }
    }
    fileInput.click();
  }

  // ── Convert File ──
  async function convertFile(file) {
    if (file.size > 50 * 1024 * 1024) {
      showToast('File too large — max 50MB', 'error');
      return;
    }

    showConverting(file.name);

    const formData = new FormData();
    formData.append('file', file);
    formData.append('engine', selectedEngine);

    try {
      const res = await fetchWithServerHint(API_CONVERT, {
        method: 'POST',
        body: formData,
        headers: { 'X-MD-Creator-Token': API_TOKEN },
      });
      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.detail || 'Conversion failed');
      }

      currentMarkdown = data.markdown || '';
      currentFilename = data.download_filename || data.filename.replace(/\.[^.]+$/, '') + '.md';
      currentDownloadId = data.download_id || '';
      currentMarkdownIsPreview = Boolean(data.markdown_is_preview || data.preview_truncated);

      showResults(data);
      addToHistory(data);
      showToast(data.preview_truncated ? 'Preview ready. Full Markdown is available for download.' : 'Conversion complete! ✨', 'success');
    } catch (err) {
      showUpload();
      showToast(err.message || 'Conversion failed', 'error');
    }
  }

  // ── UI States ──
  function showConverting(filename) {
    dropZone.style.display = 'none';
    heroSection.style.display = 'none';
    $('#engine-selector').style.display = 'none';
    convertingEl.classList.add('active');
    resultsEl.classList.remove('active');
    convertingFilename.textContent = filename;
  }

  function showResults(data) {
    convertingEl.classList.remove('active');
    resultsEl.classList.add('active');

    resultsFilename.textContent = data.filename;
    resultsSize.textContent = formatBytes(data.file_size);
    resultsTime.textContent = `\u26A1 ${data.conversion_time}s`;
    resultsChars.textContent = `${data.markdown_length.toLocaleString()} chars${data.preview_truncated ? ' full' : ''}`;

    // Engine badge
    const eng = data.engine || 'standard';
    const engLabel = eng === 'academic' ? 'Academic' : 'Standard';
    const existingBadge = document.querySelector('.engine-badge');
    if (existingBadge) existingBadge.remove();
    const badge = document.createElement('span');
    badge.className = `engine-badge ${eng}`;
    badge.textContent = engLabel;
    resultsFilename.parentNode.insertBefore(badge, resultsSize);

    // Fallback warning
    const warnings = Array.isArray(data.warnings) ? data.warnings : (data.warning ? [data.warning] : []);
    warnings.forEach((warning) => showToast(warning, 'error'));

    const markdown = data.markdown || '';
    rawPane.textContent = markdown;
    if (markdown.length <= PREVIEW_RENDER_LIMIT) {
      previewPane.innerHTML = mdit.render(markdown);
    } else {
      previewPane.textContent = 'Preview omitted for large Markdown output. Download the full file or use the raw pane.';
    }

    const lines = (markdown.match(/\n/g) || []).length + 1;
    lineCount.textContent = `${lines} lines`;
  }

  function showUpload() {
    convertingEl.classList.remove('active');
    resultsEl.classList.remove('active');
    heroSection.style.display = '';
    dropZone.style.display = '';
    $('#engine-selector').style.display = '';
  }

  // ── Buttons ──
  function setupButtons() {
    $('#btn-copy').addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(currentMarkdown);
        showToast(currentMarkdownIsPreview ? 'Copied preview to clipboard.' : 'Copied to clipboard! 📋', 'success');
      } catch {
        fallbackCopy(currentMarkdown);
      }
    });

    $('#btn-download').addEventListener('click', async () => {
      try {
        let blob;
        if (currentDownloadId) {
          const res = await fetchWithServerHint(`/api/download/${encodeURIComponent(currentDownloadId)}`, {
            headers: { 'X-MD-Creator-Token': API_TOKEN },
          });
          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || 'Download failed');
          }
          blob = await res.blob();
        } else {
          blob = new Blob([currentMarkdown], { type: 'text/markdown;charset=utf-8' });
        }
        triggerDownload(blob, currentFilename);
        showToast('Downloaded! 💾', 'success');
      } catch (err) {
        showToast(err.message || 'Download failed', 'error');
      }
    });

    $('#btn-new').addEventListener('click', showUpload);
  }

  function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    showToast('Copied to clipboard! 📋', 'success');
  }

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  async function fetchWithServerHint(url, options) {
    try {
      return await fetch(url, options);
    } catch (err) {
      if (err instanceof TypeError) {
        throw new Error('Cannot reach the local converter server. Restart with start.bat, then refresh this page.');
      }
      throw err;
    }
  }

  // ── Tabs ──
  function setupTabs() {
    document.querySelectorAll('.tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');

        const mode = tab.dataset.tab;
        const paneRawEl = $('#pane-raw');
        const panePreviewEl = $('#pane-preview');

        editorContainer.classList.remove('single-pane');
        paneRawEl.style.display = '';
        panePreviewEl.style.display = '';

        if (mode === 'raw') {
          editorContainer.classList.add('single-pane');
          panePreviewEl.style.display = 'none';
        } else if (mode === 'preview') {
          editorContainer.classList.add('single-pane');
          paneRawEl.style.display = 'none';
        }
      });
    });
  }

  // ── History ──
  function setupHistory() {
    $('#btn-history').addEventListener('click', () => {
      renderHistory();
      historyOverlay.classList.add('active');
    });
    $('#history-close').addEventListener('click', closeHistory);
    historyOverlay.addEventListener('click', (e) => {
      if (e.target === historyOverlay) closeHistory();
    });
    $('#btn-clear-history').addEventListener('click', () => {
      localStorage.removeItem(HISTORY_KEY);
      renderHistory();
      showToast('History cleared', 'success');
    });
  }

  function closeHistory() {
    historyOverlay.classList.remove('active');
  }

  function getHistory() {
    try {
      return JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
    } catch {
      return [];
    }
  }

  function addToHistory(data) {
    try {
      const history = getHistory();
      const saveMarkdown = typeof data.markdown === 'string' &&
        data.markdown.length <= HISTORY_MARKDOWN_LIMIT &&
        !data.markdown_is_preview &&
        !data.preview_truncated;
      history.unshift({
        filename: data.filename,
        extension: data.extension,
        file_size: data.file_size,
        markdown_length: data.markdown_length,
        conversion_time: data.conversion_time,
        markdown: saveMarkdown ? data.markdown : null,
        markdown_omitted: !saveMarkdown,
        preview_truncated: Boolean(data.preview_truncated),
        timestamp: new Date().toISOString(),
      });
      if (history.length > MAX_HISTORY) history.pop();
      localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
      if (!saveMarkdown) {
        showToast(data.preview_truncated ? 'Preview shown, but full Markdown was not saved to history.' : 'Large result shown, but full Markdown was not saved to history.', 'info');
      }
    } catch (err) {
      console.warn('History save failed:', err);
      showToast('Result shown, but history was not saved.', 'info');
    }
  }

  function renderHistory() {
    const history = getHistory();
    if (history.length === 0) {
      historyList.innerHTML = '<div class="history-empty">No conversions yet.<br>Drop a file to get started!</div>';
      return;
    }
    historyList.innerHTML = history
      .map(
        (item, i) => `
      <div class="history-item" data-index="${i}">
        <div class="history-item-name">${escapeHtml(item.filename)}</div>
        <div class="history-item-meta">${formatBytes(item.file_size)} · ${item.markdown_length.toLocaleString()} chars · ${timeAgo(item.timestamp)}${item.markdown_omitted ? ' · full text not saved' : ''}</div>
      </div>`
      )
      .join('');

    historyList.querySelectorAll('.history-item').forEach((el) => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.index, 10);
        const item = history[idx];
        if (item.markdown === null || item.markdown === undefined) {
          showToast('Full Markdown was not saved for this large history item.', 'error');
          return;
        }
        currentMarkdown = item.markdown;
        currentFilename = item.filename.replace(/\.[^.]+$/, '') + '.md';
        currentDownloadId = '';
        currentMarkdownIsPreview = false;
        showResults({
          filename: item.filename,
          file_size: item.file_size,
          conversion_time: item.conversion_time,
          markdown: item.markdown,
          markdown_length: item.markdown_length,
        });
        closeHistory();
      });
    });
  }

  // ── Toast ──
  function showToast(message, type = 'info') {
    const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️';
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<span>${icon}</span><span>${escapeHtml(message)}</span>`;
    toastContainer.appendChild(toast);
    setTimeout(() => toast.remove(), 3200);
  }

  // ── Utils ──
  function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  }

  function timeAgo(dateStr) {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // ── Boot ──
  document.addEventListener('DOMContentLoaded', init);
})();
