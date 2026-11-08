package com.sharex.app

import android.media.MediaPlayer

object MusicPlayer {
    var mediaPlayer: MediaPlayer? = null
    var currentSong: MusicFile? = null
    var currentIndex: Int = -1
    var isPlaying: Boolean = false

    var onSongChanged: ((MusicFile) -> Unit)? = null
    var onPlayStateChanged: ((Boolean) -> Unit)? = null
    var currentList: List<MusicFile>? = null

    fun playSong(song: MusicFile, index: Int, list: List<MusicFile>) {
        currentList = list
        currentIndex = index
        currentSong = song
        
        mediaPlayer?.release()
        mediaPlayer = MediaPlayer().apply {
            setDataSource(song.path)
            prepare()
            start()
            setOnCompletionListener { playNext() }
        }
        isPlaying = true
        
        onSongChanged?.invoke(song)
        onPlayStateChanged?.invoke(true)
    }

    fun togglePlayPause() {
        mediaPlayer?.let {
            if (it.isPlaying) {
                it.pause()
                isPlaying = false
            } else {
                it.start()
                isPlaying = true
            }
            onPlayStateChanged?.invoke(isPlaying)
        }
    }

    fun playNext() {
        val list = currentList ?: return
        if (list.isEmpty()) return
        val nextIndex = (currentIndex + 1) % list.size
        playSong(list[nextIndex], nextIndex, list)
    }
}