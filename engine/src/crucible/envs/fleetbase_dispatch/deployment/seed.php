<?php
require '/fleetbase/api/vendor/autoload.php';
$app = require '/fleetbase/api/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
use Fleetbase\Models\Company;
use Fleetbase\Models\User;
use Fleetbase\Models\ApiCredential;
use Fleetbase\FleetOps\Support\FleetOps;
$company = Company::firstOrCreate(['name' => 'Crucible Logistics']);
$user = User::firstOrCreate(['email' => 'operator@crucible.invalid'], ['name' => 'Crucible Operator', 'company_uuid' => $company->uuid, 'status' => 'active']);
$company->setOwner($user, true)->save();
$user->assignCompany($company, 'Administrator');
$user->assignSingleRole('Administrator');
FleetOps::createTransportConfig($company);
Fleetbase\Models\Setting::configure('fleetops.auto_reallocate_on_complete', false);
Fleetbase\Support\ApiRateLimits::store(['enabled' => false, 'track_consumers' => false]);
Fleetbase\FleetOps\Support\Routing\RoutingProviderPolicy::save(['server_routing' => 'straight_line']);
$credential = ApiCredential::firstOrCreate(['name' => 'crucible-private-operator', 'company_uuid' => $company->uuid, 'user_uuid' => $user->uuid]);
if ($providedKey = getenv('CRUCIBLE_FLEETBASE_API_KEY')) {
    if (!preg_match('/^flb_live_[A-Za-z0-9]{32,}$/', $providedKey)) {
        throw new RuntimeException('CRUCIBLE_FLEETBASE_API_KEY must be a private flb_live_ key');
    }
    $credential->key = $providedKey;
    $credential->secret = Illuminate\Support\Facades\Hash::make(substr($providedKey, 9));
    $credential->save();
}
file_put_contents('/tmp/crucible-private-token', $credential->key);
chmod('/tmp/crucible-private-token', 0600);
echo json_encode(['company' => $company->uuid, 'user' => $user->uuid, 'token_file' => '/tmp/crucible-private-token'])."\n";
