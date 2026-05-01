/**
 * MD_CREATOR — Client-side Application Logic
 * Handles file upload, conversion API calls, preview rendering, history, and UI interactions.
 */

(function () {
  'use strict';

  // ── Config ──
  const API_CONVERT = '/api/convert';
  const API_FORMATS = '/api/formats';
  const HISTORY_KEY = 'mdcreator_history';
  const MAX_HISTORY = 20;

  // ── State ──
  let currentMarkdown = '';
  let currentFilename = '';

  // ── markdown-it instance ──
  const mdit = window.markdownit({
    html: true,
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
    setupDragDrop();
    setupButtons();
    setupTabs();
    setupHistory();
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
    dropZone.addEventListener('click', () => fileInput.click());
    browseTrigger.addEventListener('click', (e) => {
      e.stopPropagation();
      fileInput.click();
    });
    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) convertFile(fileInput.files[0]);
      fileInput.value = '';
    });
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

    try {
      const res = await fetch(API_CONVERT, { method: 'POST', body: formData });
      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.detail || 'Conversion failed');
      }

      currentMarkdown = data.markdown;
      currentFilename = data.filename.replace(/\.[^.]+$/, '') + '.md';

      showResults(data);
      addToHistory(data);
      showToast('Conversion complete! ✨', 'success');
    } catch (err) {
      showUpload();
      showToast(err.message || 'Conversion failed', 'error');
    }
  }

  // ── UI States ──
  function showConverting(filename) {
    dropZone.style.display = 'none';
    heroSection.style.display = 'none';
    convertingEl.classList.add('active');
    resultsEl.classList.remove('active');
    convertingFilename.textContent = filename;
  }

  function showResults(data) {
    convertingEl.classList.remove('active');
    resultsEl.classList.add('active');

    resultsFilename.textContent = data.filename;
    resultsSize.textContent = formatBytes(data.file_size);
    resultsTime.textContent = `⚡ ${data.conversion_time}s`;
    resultsChars.textContent = `${data.markdown_length.toLocaleString()} chars`;

    rawPane.textContent = data.markdown;
    previewPane.innerHTML = mdit.render(data.markdown);

    const lines = (data.markdown.match(/\n/g) || []).length + 1;
    lineCount.textContent = `${lines} lines`;
  }

  function showUpload() {
    convertingEl.classList.remove('active');
    resultsEl.classList.remove('active');
    heroSection.style.display = '';
    dropZone.style.display = '';
  }

  // ── Buttons ──
  function setupButtons() {
    $('#btn-copy').addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(currentMarkdown);
        showToast('Copied to clipboard! 📋', 'success');
      } catch {
        fallbackCopy(currentMarkdown);
      }
    });

    $('#btn-download').addEventListener('click', () => {
      const blob = new Blob([currentMarkdown], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = currentFilename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast('Downloaded! 💾', 'success');
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
    const history = getHistory();
    history.unshift({
      filename: data.filename,
      extension: data.extension,
      file_size: data.file_size,
      markdown_length: data.markdown_length,
      conversion_time: data.conversion_time,
      markdown: data.markdown,
      timestamp: new Date().toISOString(),
    });
    if (history.length > MAX_HISTORY) history.pop();
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
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
        <div class="history-item-meta">${formatBytes(item.file_size)} · ${item.markdown_length.toLocaleString()} chars · ${timeAgo(item.timestamp)}</div>
      </div>`
      )
      .join('');

    historyList.querySelectorAll('.history-item').forEach((el) => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.index, 10);
        const item = history[idx];
        currentMarkdown = item.markdown;
        currentFilename = item.filename.replace(/\.[^.]+$/, '') + '.md';
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
