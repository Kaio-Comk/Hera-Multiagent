# tests/test_chat_imagens.py
'''Testes para cli/chat_imagens.py.'''

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cli.chat_imagens import (
    EXTENSOES,
    MODELOS_COM_VISAO,
    Imagem,
    _mime_pelos_bytes,
    escolher_modelo_visao,
    extrair_imagens,
    imagem_da_area_de_transferencia,
    modelo_enxerga,
    montar_conteudo,
)

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
JPEG = b'\xff\xd8\xff' + b'\x00' * 32
GIF = b'GIF89a' + b'\x00' * 32
WEBP = b'RIFF\x00\x00\x00\x00WEBPVP8 ' + b'\x00' * 32


def _criar(tmp_path: Path, nome: str, dados: bytes) -> Path:
    p = tmp_path / nome
    p.write_bytes(dados)
    return p


def test_mime_pelos_bytes():
    assert _mime_pelos_bytes(PNG) == 'image/png'
    assert _mime_pelos_bytes(JPEG) == 'image/jpeg'
    assert _mime_pelos_bytes(GIF) == 'image/gif'
    assert _mime_pelos_bytes(WEBP) == 'image/webp'
    assert _mime_pelos_bytes(b'texto qualquer') is None


def test_extensoes_mapeamento():
    assert EXTENSOES['.png'] == 'image/png'
    assert EXTENSOES['.jpg'] == 'image/jpeg'
    assert EXTENSOES['.jpeg'] == 'image/jpeg'
    assert EXTENSOES['.gif'] == 'image/gif'
    assert EXTENSOES['.webp'] == 'image/webp'


def test_imagem_tamanho_kb():
    img = Imagem(caminho='x.png', nome='x.png', mime='image/png', dados=b'\x00' * 2048)
    assert img.tamanho_kb == pytest.approx(2.0)


def test_extrair_mencao_at_simples(tmp_path):
    _criar(tmp_path, 'foto.png', PNG)
    texto, imgs, avisos = extrair_imagens('veja @foto.png', tmp_path)
    assert texto == 'veja [imagem: foto.png]'
    assert len(imgs) == 1
    assert imgs[0].mime == 'image/png'
    assert avisos == []


def test_extrair_mencao_at_com_aspas(tmp_path):
    _criar(tmp_path, 'minha foto.png', PNG)
    texto, imgs, avisos = extrair_imagens('veja @"minha foto.png"', tmp_path)
    assert texto == 'veja [imagem: minha foto.png]'
    assert len(imgs) == 1
    assert avisos == []


def test_extrair_caminho_windows_aspas(tmp_path):
    alvo = _criar(tmp_path, 'win.png', PNG)
    texto_original = f'olha "{alvo}"'
    texto, imgs, avisos = extrair_imagens(texto_original, tmp_path)
    assert texto == 'olha [imagem: win.png]'
    assert len(imgs) == 1
    assert avisos == []


def test_extrair_caminho_windows_sem_aspas(tmp_path):
    # tmp_path tem espaco no Windows (nome do usuario); o caso sem aspas vale para caminhos sem espaco.
    texto, imgs, avisos = extrair_imagens(r'olha C:\hera_inexistente\win2.png agora', tmp_path)
    assert imgs == []
    assert avisos == [r'imagem nao encontrada: C:\hera_inexistente\win2.png']
    assert texto == r'olha C:\hera_inexistente\win2.png agora'


def test_extrair_caminho_windows_com_aspas_e_espacos(tmp_path):
    alvo = _criar(tmp_path, 'win3.png', PNG)
    texto, imgs, avisos = extrair_imagens(f'olha "{alvo}"', tmp_path)
    assert texto == 'olha [imagem: win3.png]'
    assert len(imgs) == 1 and avisos == []


def test_extrair_txt_renomeado_png(tmp_path):
    _criar(tmp_path, 'falso.png', b'isto nao e imagem')
    texto, imgs, avisos = extrair_imagens('veja @falso.png', tmp_path)
    assert texto == 'veja @falso.png'
    assert imgs == []
    assert any('formato de imagem invalido' in a for a in avisos)


def test_extrair_arquivo_grande(tmp_path):
    grande = PNG + b'\x00' * 100
    _criar(tmp_path, 'grande.png', grande)
    texto, imgs, avisos = extrair_imagens(
        'veja @grande.png', tmp_path, max_bytes=50
    )
    assert texto == 'veja @grande.png'
    assert imgs == []
    assert any('grande demais' in a for a in avisos)


def test_extrair_nao_encontrado(tmp_path):
    texto, imgs, avisos = extrair_imagens('veja @nao_existe.png', tmp_path)
    assert texto == 'veja @nao_existe.png'
    assert imgs == []
    assert any('imagem nao encontrada' in a for a in avisos)


def test_extrair_duplicada(tmp_path):
    _criar(tmp_path, 'dup.png', PNG)
    texto, imgs, avisos = extrair_imagens('@dup.png e @dup.png', tmp_path)
    assert len(imgs) == 1
    assert texto == '[imagem: dup.png] e [imagem: dup.png]'


def test_extrair_limite_imagens(tmp_path):
    for i in range(5):
        _criar(tmp_path, f'img{i}.png', PNG)
    texto, imgs, avisos = extrair_imagens(
        '@img0.png @img1.png @img2.png @img3.png @img4.png',
        tmp_path,
        max_imagens=2,
    )
    assert len(imgs) == 2
    assert any('limite de 2 imagens' in a for a in avisos)


def test_extrair_clip_sem_funcao(tmp_path):
    texto, imgs, avisos = extrair_imagens('veja @clip', tmp_path)
    assert texto == 'veja @clip'
    assert imgs == []
    assert 'area de transferencia sem imagem' in avisos


def test_extrair_clip_com_funcao(tmp_path):
    alvo = _criar(tmp_path, 'clip.png', PNG)

    def exportar():
        return alvo

    texto, imgs, avisos = extrair_imagens(
        'veja @clip', tmp_path, exportar_clipboard=exportar
    )
    assert texto == 'veja [imagem: clip.png]'
    assert len(imgs) == 1
    assert avisos == []


def test_extrair_clip_retorna_none(tmp_path):
    def exportar():
        return None

    texto, imgs, avisos = extrair_imagens(
        'veja @clipboard', tmp_path, exportar_clipboard=exportar
    )
    assert texto == 'veja @clipboard'
    assert imgs == []
    assert 'area de transferencia sem imagem' in avisos


def test_montar_conteudo_sem_imagens():
    assert montar_conteudo('ola', []) == 'ola'


def test_montar_conteudo_com_imagens():
    img = Imagem(caminho='x.png', nome='x.png', mime='image/png', dados=PNG)
    resultado = montar_conteudo('ola', [img])
    assert isinstance(resultado, list)
    assert resultado[0] == {'type': 'text', 'text': 'ola'}
    assert resultado[1]['type'] == 'image_url'
    assert resultado[1]['image_url']['url'].startswith('data:image/png;base64,')


def test_modelo_enxerga():
    assert modelo_enxerga('deepseek-ai/DeepSeek-V4.1-Flash')
    assert modelo_enxerga('gemini-flash-latest')
    assert modelo_enxerga('algum-gemini-pro')
    assert modelo_enxerga('claude-3-opus')
    assert modelo_enxerga('gpt-4o-mini')
    assert not modelo_enxerga('llama-2')
    assert not modelo_enxerga('')


def test_escolher_modelo_visao_preferido():
    disp = ['outro', 'gemini-flash-lite-latest', 'deepseek-ai/DeepSeek-V4.1-Flash']
    assert escolher_modelo_visao(disp) == 'deepseek-ai/DeepSeek-V4.1-Flash'


def test_escolher_modelo_visao_fallback():
    disp = ['outro', 'claude-3-sonnet']
    assert escolher_modelo_visao(disp) == 'claude-3-sonnet'


def test_escolher_modelo_visao_nenhum():
    assert escolher_modelo_visao(['llama-2', 'mistral']) is None


def test_escolher_modelo_visao_indisponivel():
    disp = ['deepseek-ai/DeepSeek-V4.1-Flash', 'gemini-flash-lite-latest']
    assert (
        escolher_modelo_visao(disp, indisponiveis=['deepseek-ai/DeepSeek-V4.1-Flash'])
        == 'gemini-flash-lite-latest'
    )


def test_imagem_clipboard_nao_windows(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert imagem_da_area_de_transferencia() is None


def test_imagem_clipboard_sucesso(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'platform', 'win32')

    def executar_falso(cmd, **kwargs):
        # o caminho do arquivo temporario vai dentro do script: $destino = '<caminho>';
        import re

        destino = re.match(r"\$destino = '(.*?)';", cmd[-1]).group(1)
        Path(destino).write_bytes(PNG)
        return subprocess.CompletedProcess(cmd, 0, b'', b'')

    resultado = imagem_da_area_de_transferencia(executar=executar_falso)
    assert resultado is not None
    assert resultado.is_file()
    assert resultado.read_bytes() == PNG
    resultado.unlink()


def test_imagem_clipboard_falha(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')

    def executar_falso(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, b'', b'')

    assert imagem_da_area_de_transferencia(executar=executar_falso) is None


def test_imagem_clipboard_excecao(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')

    def executar_falso(cmd, **kwargs):
        raise OSError('falha')

    assert imagem_da_area_de_transferencia(executar=executar_falso) is None


def test_caminho_do_windows_com_espacos_e_sem_aspas(tmp_path):
    alvo = _criar(tmp_path, 'Capturar.PNG', PNG)
    texto, imgs, avisos = extrair_imagens(f'nada acontece {alvo} e agora?', tmp_path)
    assert texto == 'nada acontece [imagem: Capturar.PNG] e agora?'
    assert len(imgs) == 1 and avisos == []


def test_dois_caminhos_sem_aspas_nao_se_misturam(tmp_path):
    a = _criar(tmp_path, 'a.png', PNG)
    b = _criar(tmp_path, 'b.png', PNG)
    texto, imgs, avisos = extrair_imagens(f'{a} versus {b}', tmp_path)
    assert texto == '[imagem: a.png] versus [imagem: b.png]'
    assert [i.nome for i in imgs] == ['a.png', 'b.png']


def test_caminho_que_nao_e_imagem_fica_como_esta(tmp_path):
    texto, imgs, avisos = extrair_imagens(r'abra C:\Users\x\notas.txt e C:\Users\y\z.png2', tmp_path)
    assert imgs == [] and avisos == [] and texto == r'abra C:\Users\x\notas.txt e C:\Users\y\z.png2'
