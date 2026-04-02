/* MemoryVault — SSE listener and shared utilities */

function watchTask(taskId, onProgress, onDone, onError) {
    const es = new EventSource(`/api/tasks/${taskId}/events`);

    es.onmessage = function(e) {
        const data = JSON.parse(e.data);
        if (onProgress) onProgress(data);
    };

    es.addEventListener('done', function(e) {
        es.close();
        const data = JSON.parse(e.data);
        if (data.error) {
            if (onError) onError(data.error);
        } else {
            if (onDone) onDone(data);
        }
    });

    es.addEventListener('error', function(e) {
        es.close();
        if (onError) onError('Connection lost');
    });

    return es;
}

function formatNumber(n) {
    return n.toLocaleString();
}

function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
    return (bytes / 1073741824).toFixed(1) + ' GB';
}
