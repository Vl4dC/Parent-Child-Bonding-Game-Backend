import learning_service.serve as serve


def test_the_address_that_reaches_the_internet_comes_first_and_junk_is_dropped():
    assert serve.order(
        "10.247.10.19", ["10.11.11.5", "127.0.0.1", "169.254.7.7", "10.247.10.19"]
    ) == ["10.247.10.19", "10.11.11.5"]


def test_works_without_a_default_route():
    assert serve.order(None, ["192.168.1.20", "127.0.1.1"]) == ["192.168.1.20"]
    assert serve.order(None, []) == []


def test_addresses_are_plain_ipv4_strings():
    for a in serve.addresses():
        assert a.count(".") == 3 and not a.startswith(("127.", "169.254."))
