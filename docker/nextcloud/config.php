<?php
$CONFIG = array (
  'htaccess.RewriteBase' => '/',
  'memcache.local' => '\\OC\\Memcache\\APCu',
  'apps_paths' => 
  array (
    0 => 
    array (
      'path' => '/var/www/html/apps',
      'url' => '/apps',
      'writable' => false,
    ),
    1 => 
    array (
      'path' => '/var/www/html/custom_apps',
      'url' => '/custom_apps',
      'writable' => true,
    ),
  ),
  'instanceid' => 'oc1vh3kho5hh',
  'passwordsalt' => 'SnqDGN2g/qLVmOiRG1L6Ht7kX5ttZo',
  'secret' => '{{ lookup('env', 'NEXTCLOUD_SECRET') }}',
  'trusted_proxies' => array('10.2.32.1'),
  'trusted_domains' => 
  array (
    0 => 'nkontur.com',
  ),
  'datadirectory' => '/data',
  'forwarded_for_headers' => array('HTTP_X_FORWARDED_FOR'),
  'dbtype' => 'mysql',
  'version' => '24.0.5',
  'overwritehost' => 'nkontur.com',
  'overwrite.cli.url' => 'https://nkontur.com/nextcloud',
  'overwriteprotocol' => 'https',
  'overwritewebroot' => '/nextcloud',
  'dbname' => 'nextcloud',
  'dbhost' => 'nextcloud_database',
  'dbport' => '',
  'dbtableprefix' => 'oc_',
  'mysql.utf8mb4' => false,
  'dbuser' => 'nextcloud',
  'dbpassword' => '{{ lookup('env', 'NEXTCLOUD_DB_PASSWORD') }}',
  'installed' => true,
  'loglevel' => 3,
  'maintenance' => false,
  'mail_from_address' => 'noah',
  'mail_smtpmode' => 'smtp',
  'mail_sendmailmode' => 'smtp',
  'mail_domain' => 'nkontur.com',
  'mail_smtphost' => 'vps.nkontur.com',
  'mail_smtpsecure' => 'ssl',
  'mail_smtpport' => '465',
  'mail_smtpauthtype' => 'PLAIN',
  'mail_smtpauth' => 1,
  'mail_smtpname' => 'noah',
  'mail_smtppassword' => '{{ lookup('env', 'SMTP_PASSWORD') }}',
  'ldapProviderFactory' => 'OCA\\User_LDAP\\LDAPProviderFactory',
  'theme' => '',
  'app_install_overwrite' => 
  array (
    0 => 'facerecognition',
  ),
);
