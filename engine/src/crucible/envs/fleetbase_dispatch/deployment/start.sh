#!/bin/sh
set -eu
cd /fleetbase/api
php artisan config:clear
php artisan mysql:createdb
php artisan migrate --force
php artisan fleetbase:seed
php artisan fleetbase:create-permissions
php /opt/crucible/seed.php
exec php artisan serve --host=0.0.0.0 --port=8000
