/**
 * MD_CREATOR — Client-side Application Logic
 * Handles file upload, conversion API calls, preview rendering, history, and UI interactions.
 */

(function () {
  'use strict';

  // ── Config ──
  const API_CONVERT = '/api/convert';
  const API_BULK_CONVERT = '/api/bulk/convert';
  const API_BULK_JOB = '/api/bulk/jobs';
  const API_FORMATS = '/api/formats';
  const HISTORY_KEY = 'mdcreator_history';
  const MAX_HISTORY = 20;
  const HISTORY_MARKDOWN_LIMIT = 1024 * 1024;
  const PREVIEW_RENDER_LIMIT = 500 * 1024;
  const MAX_SINGLE_FILE_SIZE = 50 * 1024 * 1024;
  const MAX_BULK_FILES = 200;
  const MAX_BULK_TOTAL_SIZE = 500 * 1024 * 1024;
  const BULK_POLL_INTERVAL_MS = 900;

  // ── State ──
  let apiToken = document.querySelector('meta[name="mdcreator-token"]')?.content || '';
  let currentMarkdown = '';
  let currentFilename = '';
  let currentDownloadId = '';
  let currentMarkdownIsPreview = false;
  let selectedEngine = 'standard';
  let selectedBulkEngine = 'standard';
  let bulkFiles = [];
  let bulkJob = null;
  let bulkPollTimer = null;

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
  const singleWorkflow = $('#single-workflow');
  const bulkWorkflow = $('#bulk-workflow');
  const btnSingleView = $('#btn-single-view');
  const btnBulkView = $('#btn-bulk-view');
  const bulkDropZone = $('#bulk-drop-zone');
  const bulkFileInput = $('#bulk-file-input');
  const bulkFolderInput = $('#bulk-folder-input');
  const bulkBrowseFiles = $('#bulk-browse-files');
  const bulkBrowseFolder = $('#bulk-browse-folder');
  const bulkSelected = $('#bulk-selected');
  const bulkCount = $('#bulk-count');
  const bulkSize = $('#bulk-size');
  const bulkFileList = $('#bulk-file-list');
  const bulkProgress = $('#bulk-progress');
  const bulkStatusText = $('#bulk-status-text');
  const bulkStatusDetail = $('#bulk-status-detail');
  const bulkResults = $('#bulk-results');
  const bulkResultsTitle = $('#bulk-results-title');
  const bulkResultsConverted = $('#bulk-results-converted');
  const bulkResultsFailed = $('#bulk-results-failed');
  const bulkResultsTotal = $('#bulk-results-total');
  const bulkResultList = $('#bulk-result-list');
  const btnBulkClear = $('#btn-bulk-clear');
  const btnBulkConvert = $('#btn-bulk-convert');
  const btnBulkDownload = $('#btn-bulk-download');
  const btnBulkNew = $('#btn-bulk-new');

  // ── Init ──
  function init() {
    loadFormats();
    setupViewSwitching();
    setupEngineSelector();
    setupDragDrop();
    setupBulkWorkflow();
    setupButtons();
    setupTabs();
    setupHistory();
  }

  // ── View Switching ──
  function setupViewSwitching() {
    btnSingleView.addEventListener('click', () => showWorkflow('single'));
    btnBulkView.addEventListener('click', () => showWorkflow('bulk'));
  }

  function showWorkflow(mode) {
    const isBulk = mode === 'bulk';
    singleWorkflow.classList.toggle('active', !isBulk);
    bulkWorkflow.classList.toggle('active', isBulk);
    btnSingleView.classList.toggle('active', !isBulk);
    btnBulkView.classList.toggle('active', isBulk);
  }

  // ── Engine Selector ──
  function setupEngineSelector() {
    document.querySelectorAll('.engine-selector').forEach((selector) => {
      selector.querySelectorAll('.engine-option').forEach((btn) => {
        btn.addEventListener('click', () => {
          selector.querySelectorAll('.engine-option').forEach((b) => b.classList.remove('active'));
          btn.classList.add('active');
          if (selector.id === 'bulk-engine-selector') {
            selectedBulkEngine = btn.dataset.engine;
          } else {
            selectedEngine = btn.dataset.engine;
          }
        });
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
      const entries = snapshotDropEntries(e.dataTransfer);
      if (entries.some((entry) => entry && entry.isDirectory)) {
        showToast('That is a folder — switch to the Bulk tab to convert whole folders.', 'error');
        return;
      }
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
    openPicker(fileInput);
  }

  function openPicker(input) {
    if (typeof input.showPicker === 'function') {
      try {
        input.showPicker();
        return;
      } catch {
        // Fall back for browsers that expose showPicker but reject it here.
      }
    }
    input.click();
  }

  // ── Bulk Workflow ──
  function setupBulkWorkflow() {
    ['dragenter', 'dragover'].forEach((evt) =>
      bulkDropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        bulkDropZone.classList.add('drag-over');
      })
    );
    ['dragleave', 'drop'].forEach((evt) =>
      bulkDropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        bulkDropZone.classList.remove('drag-over');
      })
    );

    bulkDropZone.addEventListener('drop', (e) => {
      // collectDroppedItems snapshots directory entries synchronously, then
      // traverses dropped folders so they bulk-convert instead of erroring.
      collectDroppedItems(e.dataTransfer)
        .then((items) => addBulkFiles(items))
        .catch(() => showToast('Could not read the dropped items. Try the browse buttons instead.', 'error'));
    });
    bulkDropZone.addEventListener('click', () => openPicker(bulkFileInput));
    bulkBrowseFiles.addEventListener('click', (e) => {
      e.stopPropagation();
      openPicker(bulkFileInput);
    });
    bulkBrowseFolder.addEventListener('click', (e) => {
      e.stopPropagation();
      openPicker(bulkFolderInput);
    });
    bulkFileInput.addEventListener('change', () => {
      addBulkFiles(fileListToItems(bulkFileInput.files));
      bulkFileInput.value = '';
    });
    bulkFolderInput.addEventListener('change', () => {
      addBulkFiles(fileListToItems(bulkFolderInput.files));
      bulkFolderInput.value = '';
    });
    btnBulkClear.addEventListener('click', clearBulkFiles);
    btnBulkConvert.addEventListener('click', startBulkConversion);
    btnBulkDownload.addEventListener('click', downloadBulkZip);
    btnBulkNew.addEventListener('click', () => {
      clearBulkFiles();
      bulkResults.classList.remove('active');
    });
  }

  function fileListToItems(fileList) {
    return Array.from(fileList || []).map((file) => ({
      file,
      path: normalizeClientRelativePath(file.webkitRelativePath || file.name),
    }));
  }

  function addBulkFiles(incoming) {
    if (bulkProgress.classList.contains('active')) {
      showToast('Bulk conversion is already running.', 'error');
      return;
    }

    if (!incoming || incoming.length === 0) return;
    bulkJob = null;
    bulkResults.classList.remove('active');

    const currentPaths = new Set(bulkFiles.map((item) => item.path));
    const nextFiles = [...bulkFiles];
    let skipped = 0;

    incoming.forEach(({ file, path }) => {
      if (!file || file.size > MAX_SINGLE_FILE_SIZE) {
        skipped += 1;
        return;
      }
      if (!path || currentPaths.has(path)) {
        skipped += 1;
        return;
      }
      currentPaths.add(path);
      nextFiles.push({ file, path });
    });

    if (nextFiles.length > MAX_BULK_FILES) {
      bulkFiles = nextFiles.slice(0, MAX_BULK_FILES);
      skipped += nextFiles.length - MAX_BULK_FILES;
      showToast(`Bulk batches are limited to ${MAX_BULK_FILES} files. Extra files were skipped.`, 'error');
    } else {
      bulkFiles = nextFiles;
    }

    if (bulkTotalSize() > MAX_BULK_TOTAL_SIZE) {
      bulkFiles = [];
      showToast('Batch too large — max 500MB total. Select a smaller set.', 'error');
    } else if (skipped > 0) {
      showToast(`${skipped} file${skipped === 1 ? '' : 's'} skipped due to size or duplicate path.`, 'error');
    }

    renderBulkSelection();
  }

  function normalizeClientRelativePath(path) {
    return String(path || '')
      .replace(/\\/g, '/')
      .replace(/^\/+/, '')
      .split('/')
      .filter((part) => part && part !== '.' && part !== '..' && !part.includes(':'))
      .join('/');
  }

  function bulkTotalSize() {
    return bulkFiles.reduce((sum, item) => sum + item.file.size, 0);
  }

  function clearBulkFiles() {
    bulkFiles = [];
    bulkJob = null;
    if (bulkPollTimer) clearTimeout(bulkPollTimer);
    bulkPollTimer = null;
    bulkProgress.classList.remove('active');
    bulkSelected.classList.remove('active');
    renderBulkSelection();
  }

  function renderBulkSelection() {
    const totalSize = bulkTotalSize();
    bulkSelected.classList.toggle('active', bulkFiles.length > 0);
    bulkCount.textContent = bulkFiles.length === 0
      ? 'No files selected'
      : `${bulkFiles.length} file${bulkFiles.length === 1 ? '' : 's'} selected`;
    bulkSize.textContent = formatBytes(totalSize);
    btnBulkConvert.disabled = bulkFiles.length === 0;
    bulkFileList.innerHTML = bulkFiles
      .map(
        (item) => `
        <div class="bulk-file-item">
          <div class="bulk-file-name">${escapeHtml(item.path)}</div>
          <div class="bulk-file-size">${formatBytes(item.file.size)}</div>
        </div>`
      )
      .join('');
  }

  async function startBulkConversion() {
    if (bulkFiles.length === 0) return;

    if (bulkPollTimer) clearTimeout(bulkPollTimer);
    bulkResults.classList.remove('active');
    bulkProgress.classList.add('active');
    bulkStatusText.textContent = 'Checking selected files...';
    bulkStatusDetail.textContent = `${bulkFiles.length} files · ${formatBytes(bulkTotalSize())}`;
    btnBulkConvert.disabled = true;

    const unreadable = [];
    for (const item of bulkFiles) {
      if (!(await isFileReadable(item.file))) unreadable.push(item.path);
    }
    if (unreadable.length > 0) {
      bulkFiles = bulkFiles.filter((item) => !unreadable.includes(item.path));
      renderBulkSelection();
      showToast(
        `Skipped ${unreadable.length} unreadable item${unreadable.length === 1 ? '' : 's'} (folders or moved files): ${unreadable[0]}${unreadable.length > 1 ? ', …' : ''}`,
        'error'
      );
      if (bulkFiles.length === 0) {
        bulkProgress.classList.remove('active');
        btnBulkConvert.disabled = true;
        return;
      }
    }

    bulkStatusText.textContent = 'Uploading bulk conversion job...';
    bulkStatusDetail.textContent = `${bulkFiles.length} files · ${formatBytes(bulkTotalSize())}`;

    const formData = new FormData();
    formData.append('engine', selectedBulkEngine);
    bulkFiles.forEach((item) => {
      formData.append('files', item.file, item.file.name);
      formData.append('paths', item.path);
    });

    try {
      const res = await apiFetch(API_BULK_CONVERT, {
        method: 'POST',
        body: formData,
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Bulk conversion failed');
      }
      bulkJob = data.job;
      renderBulkJob(bulkJob);
      pollBulkJob(bulkJob.id);
    } catch (err) {
      bulkProgress.classList.remove('active');
      btnBulkConvert.disabled = bulkFiles.length === 0;
      showToast(err.message || 'Bulk conversion failed', 'error');
    }
  }

  async function pollBulkJob(jobId) {
    try {
      const res = await apiFetch(`${API_BULK_JOB}/${encodeURIComponent(jobId)}`);
      const data = await res.json();
      if (!res.ok) {
        if (res.status === 404) {
          throw new Error('This bulk job is no longer tracked (the server restarted). Re-run the batch.');
        }
        throw new Error(data.detail || 'Could not read bulk job status');
      }
      bulkJob = data.job;
      renderBulkJob(bulkJob);
      if (bulkJob.status === 'queued' || bulkJob.status === 'running') {
        bulkPollTimer = setTimeout(() => pollBulkJob(jobId), BULK_POLL_INTERVAL_MS);
      }
    } catch (err) {
      bulkProgress.classList.remove('active');
      btnBulkConvert.disabled = bulkFiles.length === 0;
      showToast(err.message || 'Could not read bulk job status', 'error');
    }
  }

  function renderBulkJob(job) {
    if (job.status === 'queued' || job.status === 'running') {
      bulkProgress.classList.add('active');
      const progress = job.progress;
      if (job.status === 'queued') {
        bulkStatusText.textContent = 'Waiting to start...';
      } else if (progress && progress.total > 0) {
        bulkStatusText.textContent = `Converting files... ${Math.min(progress.done, progress.total)}/${progress.total}`;
      } else {
        bulkStatusText.textContent = 'Converting files...';
      }
      const currentLabel = progress && progress.current ? ` · ${progress.current}` : '';
      bulkStatusDetail.textContent = `${job.file_count || bulkFiles.length} files · ${formatBytes(job.total_size || bulkTotalSize())}${currentLabel}`;
      return;
    }

    bulkProgress.classList.remove('active');
    btnBulkConvert.disabled = bulkFiles.length === 0;

    if (job.status === 'failed') {
      showToast(job.error || 'Bulk conversion failed', 'error');
      return;
    }

    renderBulkResults(job);
  }

  function renderBulkResults(job) {
    const summary = job.summary || {};
    const failed = summary.failed || 0;
    bulkResults.classList.add('active');
    bulkResultsTitle.textContent = job.download_filename || 'Bulk conversion';
    bulkResultsConverted.textContent = `${summary.converted || 0} converted`;
    bulkResultsFailed.textContent = `${failed} failed`;
    bulkResultsTotal.textContent = `${summary.total || 0} total`;
    btnBulkDownload.disabled = !job.download_url;
    bulkResultList.innerHTML = (summary.results || [])
      .map((item) => {
        const status = item.status || 'unknown';
        const detail = item.error || item.warning || item.output || status;
        return `
          <div class="bulk-result-item ${escapeHtml(status)}">
            <div class="bulk-result-name">${escapeHtml(item.source || item.output || 'unknown')}</div>
            <div class="bulk-result-meta">${escapeHtml(status)} · ${formatBytes(item.file_size || 0)}</div>
            ${item.error ? `<div class="bulk-result-error">${escapeHtml(detail)}</div>` : ''}
          </div>`;
      })
      .join('');
    showToast(failed > 0 ? `Bulk finished with ${failed} failed file${failed === 1 ? '' : 's'}.` : 'Bulk conversion complete!', failed > 0 ? 'error' : 'success');
  }

  async function downloadBulkZip() {
    if (!bulkJob || !bulkJob.download_url) return;
    try {
      const res = await apiFetch(bulkJob.download_url);
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || 'Bulk download failed');
      }
      const blob = await res.blob();
      triggerDownload(blob, bulkJob.download_filename || 'mdcreator-bulk.zip');
      showToast('Bulk ZIP downloaded! 💾', 'success');
    } catch (err) {
      showToast(err.message || 'Bulk download failed', 'error');
    }
  }

  // ── Convert File ──
  async function convertFile(file) {
    if (file.size > MAX_SINGLE_FILE_SIZE) {
      showToast('File too large — max 50MB', 'error');
      return;
    }

    if (!(await isFileReadable(file))) {
      showToast(`Cannot read "${file.name}". If it is a folder, use the Bulk tab; otherwise check the file still exists.`, 'error');
      return;
    }

    showConverting(file.name);

    const formData = new FormData();
    formData.append('file', file);
    formData.append('engine', selectedEngine);

    try {
      const res = await apiFetch(API_CONVERT, {
        method: 'POST',
        body: formData,
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
          const res = await apiFetch(`/api/download/${encodeURIComponent(currentDownloadId)}`);
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
        // Chrome reports a dead upload stream (folder pseudo-file, or a file
        // deleted/changed after selection) the same way as a dead server.
        // Probe health to tell the user the truth.
        let serverUp = false;
        try {
          serverUp = (await fetch('/api/health', { cache: 'no-store' })).ok;
        } catch {
          // Server really is unreachable.
        }
        if (serverUp) {
          throw new Error('Upload failed: a selected file changed or was deleted after it was picked. Re-select the files and try again.');
        }
        throw new Error('Cannot reach the local converter server. Restart with start.bat, then refresh this page.');
      }
      throw err;
    }
  }

  // The session token is minted per server start. If the server restarts while
  // this tab stays open, re-read the token from the served page and retry once
  // instead of stranding the user with 403s.
  async function refreshApiToken() {
    try {
      const res = await fetch('/', { cache: 'no-store' });
      const text = await res.text();
      const match = text.match(/name="mdcreator-token" content="([^"]+)"/);
      if (match && match[1] && match[1] !== '__MD_CREATOR_TOKEN__') {
        apiToken = match[1];
        return true;
      }
    } catch {
      // Server unreachable — the caller's error path covers this.
    }
    return false;
  }

  async function apiFetch(url, options = {}) {
    const send = () =>
      fetchWithServerHint(url, {
        ...options,
        headers: { ...(options.headers || {}), 'X-MD-Creator-Token': apiToken },
      });
    let res = await send();
    if (res.status === 403 && (await refreshApiToken())) {
      res = await send();
    }
    return res;
  }

  // Folders dropped from Explorer arrive as unreadable pseudo-files; probing a
  // one-byte slice catches them (and files deleted/moved since selection)
  // before the upload dies mid-flight with a misleading network error.
  async function isFileReadable(file) {
    try {
      await file.slice(0, 1).arrayBuffer();
      return true;
    } catch {
      return false;
    }
  }

  function snapshotDropEntries(dataTransfer) {
    // webkitGetAsEntry is only valid synchronously inside the drop event.
    return Array.from(dataTransfer.items || []).map((item) =>
      typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null
    );
  }

  async function collectDroppedItems(dataTransfer) {
    const entries = snapshotDropEntries(dataTransfer);
    const plainFiles = Array.from(dataTransfer.files || []);
    if (!entries.some(Boolean)) {
      return plainFiles.map((file) => ({ file, path: normalizeClientRelativePath(file.name) }));
    }
    const collected = [];
    for (const entry of entries) {
      if (!entry) continue;
      await walkDroppedEntry(entry, collected);
      if (collected.length > MAX_BULK_FILES) break;
    }
    return collected;
  }

  async function walkDroppedEntry(entry, collected) {
    if (collected.length > MAX_BULK_FILES) return;
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject)).catch(() => null);
      if (file) {
        collected.push({ file, path: normalizeClientRelativePath(entry.fullPath || file.name) });
      }
      return;
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      while (collected.length <= MAX_BULK_FILES) {
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject)).catch(() => []);
        if (!batch.length) break;
        for (const child of batch) {
          await walkDroppedEntry(child, collected);
        }
      }
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
