<?php
// This file is part of the local_nexa_integration plugin.
//
// Nexa Chatbot Integration - embeds the Nexa AI assistant into Moodle and,
// since 2.0, synchronises approved course content into the NEXA Knowledge
// Service (NEXA-M2-007, contract nexa.lms-sync/1.0).

defined('MOODLE_INTERNAL') || die();

$plugin->component = 'local_nexa_integration';
$plugin->version   = 2026111200;
$plugin->requires  = 2022041900; // Moodle 4.0+.
$plugin->maturity  = MATURITY_STABLE;
$plugin->release   = '2.0.2';
