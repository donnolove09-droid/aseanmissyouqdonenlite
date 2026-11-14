<?php
// admin.php - Web Admin nhận hit từ bot ASEAN MISSYOU
// Đặt file này lên hosting, set ADMIN_WEB_URL = https://domain.com/admin.php

header('Content-Type: application/json; charset=utf-8');

$dataFile = __DIR__ . '/hits.json';

// Nhận POST từ bot
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $raw = file_get_contents('php://input');
    $data = json_decode($raw, true);
    if (!$data || empty($data['account'])) {
        http_response_code(400);
        echo json_encode(['ok' => false]);
        exit;
    }

    $hits = [];
    if (file_exists($dataFile)) {
        $hits = json_decode(file_get_contents($dataFile), true) ?: [];
    }

    array_unshift($hits, $data);
    // Giữ tối đa 500 bản ghi
    $hits = array_slice($hits, 0, 500);
    file_put_contents($dataFile, json_encode($hits, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));

    echo json_encode(['ok' => true]);
    exit;
}

// Nếu GET → chuyển sang trang xem
header('Location: view.php');
exit;
