"""SSRF 防护测试

测试场景：
1. 阻止本机地址 (127.0.0.1, localhost)
2. 阻止私有网络地址 (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
3. 阻止 AWS 元数据接口 (169.254.169.254)
4. 阻止解析到内网的域名
5. 允许公网地址
6. 允许非 HTTP 协议 (magnet, ed2k)
"""
import pytest


class TestSSRFProtection:
    """SSRF 防护测试套件"""

    def test_block_localhost_ip(self, authenticated_client):
        """测试阻止 127.0.0.1"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://127.0.0.1:8080/file.zip"}
        )
        assert response.status_code == 400
        assert "本机地址" in response.json()["detail"]

    def test_block_localhost_name(self, authenticated_client):
        """测试阻止 localhost"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://localhost:8080/file.zip"}
        )
        assert response.status_code == 400
        assert "本机地址" in response.json()["detail"]

    def test_block_localhost_ipv6(self, authenticated_client):
        """测试阻止 IPv6 回环地址"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://[::1]:8080/file.zip"}
        )
        assert response.status_code == 400
        assert "本机地址" in response.json()["detail"]

    def test_block_private_network_192(self, authenticated_client):
        """测试阻止 192.168.x.x 私有网络"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://192.168.1.1/file.zip"}
        )
        assert response.status_code == 400
        assert "内网地址" in response.json()["detail"]

    def test_block_private_network_10(self, authenticated_client):
        """测试阻止 10.x.x.x 私有网络"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://10.0.0.1/file.zip"}
        )
        assert response.status_code == 400
        assert "内网地址" in response.json()["detail"]

    def test_block_private_network_172(self, authenticated_client):
        """测试阻止 172.16-31.x.x 私有网络"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://172.16.0.1/file.zip"}
        )
        assert response.status_code == 400
        assert "内网地址" in response.json()["detail"]

    def test_block_aws_metadata(self, authenticated_client):
        """测试阻止 AWS 元数据接口 (169.254.169.254)"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://169.254.169.254/latest/meta-data/"}
        )
        assert response.status_code == 400
        assert "内网地址" in response.json()["detail"]

    def test_block_zero_address(self, authenticated_client):
        """测试阻止 0.0.0.0"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://0.0.0.0:8080/file.zip"}
        )
        assert response.status_code == 400
        assert "本机地址" in response.json()["detail"]

    @pytest.mark.skip(reason="需要真实的 DNS 环境才能测试域名解析")
    def test_block_domain_resolves_to_private_ip(self, authenticated_client):
        """测试阻止解析到内网 IP 的域名

        注意：此测试需要一个真实的域名解析到内网 IP，在测试环境中难以模拟
        """
        # 假设 internal.example.com 解析到 192.168.1.1
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://internal.example.com/file.zip"}
        )
        assert response.status_code == 400
        assert "解析到内网地址" in response.json()["detail"]

    def test_allow_public_ip(self, authenticated_client):
        """测试允许公网 IP 地址

        注意：此测试会真实创建任务（如果磁盘空间足够），但任务可能会失败
        """
        # 8.8.8.8 是 Google DNS，是公网地址
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://8.8.8.8/file.zip"}
        )
        # 应该通过 SSRF 检查（不返回 400），但可能因为其他原因失败
        # 只要不是 400 且不包含 "内网" 或 "本机" 就说明通过了 SSRF 检查
        if response.status_code == 400:
            detail = response.json().get("detail", "")
            assert "内网" not in detail and "本机" not in detail

    def test_allow_public_domain(self, authenticated_client):
        """测试允许公网域名

        注意：此测试会真实创建任务（如果磁盘空间足够），但任务可能会失败
        """
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "http://example.com/file.zip"}
        )
        # 应该通过 SSRF 检查（不返回 400），但可能因为其他原因失败
        if response.status_code == 400:
            detail = response.json().get("detail", "")
            assert "内网" not in detail and "本机" not in detail

    def test_allow_magnet_link(self, authenticated_client):
        """测试允许磁力链接（不进行 SSRF 检查）"""
        # magnet 链接不经过 HTTP，不应该被 SSRF 检查拦截
        magnet_uri = "magnet:?xt=urn:btih:1234567890abcdef&dn=test"
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": magnet_uri}
        )
        # 应该通过 SSRF 检查，但可能因为其他原因失败
        if response.status_code == 400:
            detail = response.json().get("detail", "")
            assert "内网" not in detail and "本机" not in detail

    def test_allow_https(self, authenticated_client):
        """测试 HTTPS 协议同样受到 SSRF 检查"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "https://127.0.0.1:8443/file.zip"}
        )
        assert response.status_code == 400
        assert "本机地址" in response.json()["detail"]

    def test_allow_ftp_public(self, authenticated_client):
        """测试 FTP 公网地址允许"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "ftp://ftp.example.com/file.zip"}
        )
        # 应该通过 SSRF 检查
        if response.status_code == 400:
            detail = response.json().get("detail", "")
            assert "内网" not in detail and "本机" not in detail

    def test_block_ftp_private(self, authenticated_client):
        """测试 FTP 私有地址阻止"""
        response = authenticated_client.post(
            "/api/tasks",
            json={"uri": "ftp://192.168.1.1/file.zip"}
        )
        assert response.status_code == 400
        assert "内网地址" in response.json()["detail"]
