import * as React from 'react'
import socket from './socket'

import {
    TextChat
} from './components/index.js'

const fetchUserData = async (setUserData) => {
    try {
        const response = await fetch('http://localhost:3002/user/me', { credentials: 'include' })
        const data = await response.json()
        if (data.channels) {
            setUserData({...data})
        }
    } catch {
        window.location.href = '/login'
        console.log('failed to connect to server')
    }
}

// const ping = (userData) => {
//     const {session, _id} = userData
//     if (session) {
//         socket.emit('register_online', {session, _id});
//     }
// }

export const Home = () => {

    const [userData, setUserData] = React.useState({})

    React.useEffect(() => {
        fetchUserData(setUserData)
    }, [setUserData])

    return <>
        <div className='backdrop'/>
        <div className="header">
            <span>Emery Chat</span>
            <button
                className="gear-icon"
                title="Settings"
                onClick={() =>  window.location.href = '/settings'}
            >
            ⚙️
          </button>
        </div>
        <div className="content_wrapper">
            {userData._id ? 
            <TextChat
                userData={userData}
                socket={socket}
            /> : <></>}
        </div>
    </>
}
