import * as React from 'react'
import socket from './socket'

import {
    TextChat
} from './components/index.js'

const fetchUserData = async (setUserData) => {
    try {
        const response = await fetch('http://localhost:3000/user/me', { credentials: 'include' })
        const data = await response.json()
        // console.log(data)
        if (data.channels) {
            setUserData({...data})
        }
    } catch {
        window.location.href = '/login'
        console.log('failed to connect to server')
    }
}

const ping = (userData) => {
    const {session, _id} = userData
    if (session) {
        socket.emit('register_online', {session, _id});
    }
}

export const Home = () => {

    const [userData, setUserData] = React.useState({})

    React.useEffect(() => {
        fetchUserData(setUserData)
    }, [setUserData])

    React.useEffect(() => {
        if (userData.channels?.length && !currentChannel._id) {
            setInterval(() => {ping(userData)}, 5000)
        }
    }, [userData.channels])

    return <>
        <div className='backdrop'/>
        <div className="header">Emery Chat</div>
        <div className="content_wrapper">
            <TextChat />
        </div>
    </>
}
