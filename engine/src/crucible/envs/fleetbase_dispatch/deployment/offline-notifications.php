<?php
// Disable transport notifications only. Native business events/jobs still execute.
$path = '/fleetbase/api/app/Providers/AppServiceProvider.php';
$source = file_get_contents($path);
$marker = '$this->configureOutboundHttpLogging();';
if (substr_count($source, $marker) !== 1) {
    throw new RuntimeException('Pinned application provider changed; audit offline transport patch');
}
$source = str_replace($marker, 'Event::listen(\\Illuminate\\Notifications\\Events\\NotificationSending::class, fn () => false);' . "\n        " . $marker, $source);
file_put_contents($path, $source);
