# Run coverage on all remediation-touched files
python -m coverage run -m pytest `
    test/hummingbot/client/test_performance.py `
    test/hummingbot/core/utils/test_kill_switch.py `
    test/hummingbot/remote_iface/test_mqtt_contract.py `
    test/hummingbot/remote_iface/test_mqtt.py `
    test/hummingbot/client/command/test_start_command.py `
    test/hummingbot/client/command/test_history_command.py `
    test/hummingbot/connector/exchange/mexc/ `
    test/hummingbot/core/data_type/test_retry_backoff_consistency.py `
    test/hummingbot/core/data_type/test_order_book_tracker.py `
    test/hummingbot/core/test_connector_manager.py `
    test/hummingbot/core/test_trading_core.py `
    test/test_quickstart_cli.py `
    -m "not quarantined and not live_api" -q

# Report — must be a single --include line
python -m coverage report --include="hummingbot/client/performance.py,hummingbot/core/utils/kill_switch.py,hummingbot/remote_iface/mqtt.py,hummingbot/remote_iface/messages.py,hummingbot/client/command/start_command.py,hummingbot/connector/exchange/mexc/mexc_order_book.py,hummingbot/connector/exchange/mexc/mexc_post_processor.py,hummingbot/core/connector_manager.py,hummingbot/core/data_type/order_book_tracker.py,hummingbot/core/data_type/order_book_tracker_data_source.py,hummingbot/core/data_type/user_stream_tracker_data_source.py,hummingbot/core/trading_core.py,bin/hummingbot_quickstart.py"
