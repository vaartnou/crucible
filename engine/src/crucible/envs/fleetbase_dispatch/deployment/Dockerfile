FROM fleetbase/fleetbase-api@sha256:75ede1bfd007ce31ac663f5438688baa802a9314399acc30254d4834e346e706
COPY offline-notifications.php /opt/crucible/offline-notifications.php
RUN php /opt/crucible/offline-notifications.php
COPY seed.php native-proof.php count-state.php start.sh /opt/crucible/
ENTRYPOINT ["docker-php-entrypoint"]
CMD ["sh", "/opt/crucible/start.sh"]
