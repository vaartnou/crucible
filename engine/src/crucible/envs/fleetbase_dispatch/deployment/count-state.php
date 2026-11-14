<?php
// Private deployment verification only: no secrets or row contents are emitted.
require '/fleetbase/api/vendor/autoload.php';
$app = require '/fleetbase/api/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$tables = ['orders', 'payloads', 'entities', 'waypoints', 'tracking_numbers', 'tracking_statuses', 'places', 'drivers', 'vehicles'];
$counts = [];
foreach ($tables as $table) {
    $counts[$table] = Illuminate\Support\Facades\DB::table($table)->whereNull('deleted_at')->count();
}
// v2: cancelled orders are still active rows until the adapter deletes them.
$counts['orders_canceled'] = Illuminate\Support\Facades\DB::table('orders')->whereNull('deleted_at')->where('status', 'canceled')->count();
$counts['users'] = Illuminate\Support\Facades\DB::table('users')->whereNull('deleted_at')->count();
echo json_encode($counts) . "\n";
$clean = $counts['users'] === 1;
foreach ($tables as $table) {
    $clean = $clean && $counts[$table] === 0;
}
exit($clean ? 0 : 1);
