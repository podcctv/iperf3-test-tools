import { apiFetch } from './api.js';


function showResult(message, isError = false) {
  const el = document.getElementById('db-result');
  el.className = `mt-4 p-3 rounded-lg text-sm font-bold ${isError ? 'bg-rose-500/20 text-rose-400' : 'bg-emerald-500/20 text-emerald-400'}`;
  el.textContent = message;
  el.classList.remove('hidden');
}

document.getElementById('clear-all-data').addEventListener('click', async () => {
  if (!confirm('⚠️ 确定要清空所有测试数据吗？\\n\\n这将删除：\\n- 所有单次测试记录\\n- 所有定时任务执行历史\\n\\n此操作不可撤销！')) return;

  try {
    const res = await apiFetch('/admin/clear_all_test_data', { method: 'POST' });
    if (!res) { showResult('✗ 请求失败: 未授权', true); return; }
    const data = await res.json();
    if (res.ok) {
      showResult(`✓ 成功清空数据：删除了 ${data.test_results_deleted || 0} 条测试记录，${data.schedule_results_deleted || 0} 条定时任务历史`);
    } else {
      showResult(`✗ 失败: ${data.detail || '未知错误'}`, true);
    }
  } catch (e) {
    showResult(`✗ 请求失败: ${e.message}`, true);
  }
});

document.getElementById('clear-schedule-results').addEventListener('click', async () => {
  if (!confirm('⚠️ 确定要清空定时任务历史吗？\\n\\n这将删除所有定时任务的执行记录。\\n\\n此操作不可撤销！')) return;

  try {
    const res = await apiFetch('/admin/clear_schedule_results', { method: 'POST' });
    if (!res) { showResult('✗ 请求失败: 未授权', true); return; }
    const data = await res.json();
    if (res.ok) {
      showResult(`✓ 成功清空定时任务历史：删除了 ${data.count || 0} 条记录`);
    } else {
      showResult(`✗ 失败: ${data.detail || '未知错误'}`, true);
    }
  } catch (e) {
    showResult(`✗ 请求失败: ${e.message}`, true);
  }
});

// TG Settings Functions
function showTgResult(message, isError = false) {
  const el = document.getElementById('tg-result');
  el.className = `text-sm p-3 rounded-lg ${isError ? 'bg-rose-500/20 text-rose-400' : 'bg-emerald-500/20 text-emerald-400'}`;
  el.textContent = message;
  el.classList.remove('hidden');
}

// Load TG settings on page load
async function loadTgSettings() {
  try {
    const res = await apiFetch('/api/settings/telegram');
    if (res && res.ok) {
      const data = await res.json();
      document.getElementById('tg-bot-token').value = data.bot_token || '';
      document.getElementById('tg-chat-id').value = data.chat_id || '';
    }
  } catch (e) { console.error('Failed to load TG settings:', e); }
}
loadTgSettings();

document.getElementById('save-tg-settings').addEventListener('click', async () => {
  const token = document.getElementById('tg-bot-token').value.trim();
  const chatId = document.getElementById('tg-chat-id').value.trim();

  try {
    const res = await apiFetch('/api/settings/telegram', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bot_token: token, chat_id: chatId })
    });
    if (!res) { showTgResult('✗ 请求失败: 未授权', true); return; }
    if (res.ok) {
      showTgResult('✓ 设置已保存');
    } else {
      const data = await res.json();
      showTgResult(`✗ 保存失败: ${data.detail || '未知错误'}`, true);
    }
  } catch (e) {
    showTgResult(`✗ 请求失败: ${e.message}`, true);
  }
});

document.getElementById('test-tg-settings').addEventListener('click', async () => {
  try {
    const res = await apiFetch('/api/settings/telegram/test', { method: 'POST' });
    if (!res) { showTgResult('✗ 请求失败: 未授权', true); return; }
    const data = await res.json();
    if (res.ok && data.success) {
      showTgResult('✓ 测试消息已发送！请检查 Telegram');
    } else {
      showTgResult(`✗ 发送失败: ${data.message || '未知错误'}`, true);
    }
  } catch (e) {
    showTgResult(`✗ 请求失败: ${e.message}`, true);
  }
});
